"""Bitcart plugin entry point for the Liquidity Helper.

Lifecycle:
  __init__       — plugin object constructed by Bitcart's loader.
  setup_app(app) — register HTTP routes (none for now).
  startup()      — register the settings schema and the
                   `settings_changed` hook so live UI edits propagate.
  worker_setup() — only fires on the worker process; this is where the
                   tick loop actually runs. We:
                     1. ensure an auth token exists for our plugin,
                     2. apply current settings onto config/liquidityhelper,
                     3. spawn run_tick_loop() as an asyncio.Task.
  shutdown()     — signal stop_event and let the in-flight tick finish.

Why settings + token live separately:
  Settings are stored in Bitcart's `settings` table as JSON under
  `plugin:liquidityhelper`. The auth token must be a real row in the
  `tokens` table so Bitcart's auth pipeline accepts it. We look it up by
  `app_id` each startup; create-or-reuse semantics.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI

# Bitcart imports. These resolve in the bitcart process; tests stub them.
from api.plugins import BasePlugin
from api import models
from api.services.crud.repositories import TokenRepository, UserRepository
from sqlalchemy import select

from .bitcart_plugin.settings_schema import (
    PluginSettings, get_settings_groups,
)
from .bitcart_plugin.settings_bridge import (
    apply_settings,
    merge_with_config,
    register_plugin_instance,
)
from .bitcart_plugin.log_endpoints import (
    build_router, install_plugin_log_sinks, build_debug_router,
)
from .bitcart_plugin.dashboard import build_router as build_dashboard_router
from .bitcart_plugin.wallet_debug import build_wallet_debug_router
from .bitcart_plugin.log_export import build_log_export_router


def _build_schema_router(auth_dependency: Any | None = None):
    """Expose the settings schema with parsed groups + tooltips to the
    admin UI. The Vue tab reads this to render section headers and
    field descriptions.

    Why not just rely on Bitcart's `/plugins/settings/{name}` endpoint:
    that returns current VALUES but not the schema's group metadata or
    rich descriptions. We need our own endpoint to surface the parsed
    grouping + descriptions for the tabbed view.
    """
    from fastapi import APIRouter, Security

    # NOTE: prefix is "/plugins/...", not "/api/plugins/..." — bitcart's
    # FastAPI app already has `root_path="/api"`, so any leading `/api/`
    # here would produce double-mounted routes at "/api/api/plugins/..."
    # that 404 through the proxy.
    router = APIRouter(prefix="/plugins/liquidityhelper/settings")
    deps = (
        [Security(auth_dependency, scopes=["server_management"])]
        if auth_dependency is not None else []
    )

    @router.get("/schema", dependencies=deps)
    async def schema() -> dict:
        """Return [{group, settings: [{name, description, default}]}]
        in declaration order — Vue iterates this to render sections."""
        from pydantic_core import PydanticUndefined
        groups_payload = []
        for group, names in get_settings_groups():
            entries = []
            for name in names:
                field = PluginSettings.model_fields[name]
                # Fields without a declared default get pydantic's
                # `PydanticUndefined` sentinel, which is not
                # JSON-serializable and crashes the response with
                # "Unable to serialize unknown type". Map it to None
                # so the UI gets a usable value.
                default = field.default
                if default is PydanticUndefined:
                    default = None
                entries.append({
                    "name": name,
                    "description": field.description or "",
                    "default": default,
                })
            groups_payload.append({
                "group": group or "Other",
                "settings": entries,
            })
        return {"groups": groups_payload}

    return router

if TYPE_CHECKING:
    from api.services.crud.repositories import SettingRepository

logger = logging.getLogger("liquidityhelper.plugin")

PLUGIN_NAME = "liquidityhelper"
PLUGIN_APP_ID = f"plugin:{PLUGIN_NAME}"
# Full control: the engine touches stores, wallets, invoices, payouts,
# and Lightning channels. Anything narrower needs constant updating as
# the engine grows.
PLUGIN_TOKEN_PERMISSIONS = ["full_control"]


def _ensure_engine_importable() -> None:
    """Make the engine modules (config, liquidityhelper, classes, ...)
    importable by their bare names.

    Bitcart loads `modules/{author}/{plugin}/plugin.py` via importlib,
    which makes plugin.py part of the `modules.{author}.{plugin}`
    package. Sibling files (liquidityhelper.py, config.py, etc.) live
    in the same directory but they are not part of that package — the
    engine refers to them as top-level modules. We add the plugin's
    own directory to sys.path so `import liquidityhelper` resolves.
    """
    plugin_dir = os.path.dirname(os.path.abspath(__file__))
    if plugin_dir not in sys.path:
        sys.path.insert(0, plugin_dir)


# When running inside the backend container, nginx isn't on 127.0.0.1
# and gunicorn listens directly at :8000 without the /api root. Match
# the resolution liquidityhelper._resolve_internal_api_url() uses so
# the bootstrap and the engine point at the same target.
BITCART_LOCAL_URL = (
    os.environ.get("LIQUIDITYHELPER_API_URL")
    or ("http://localhost:8000" if os.environ.get("BITCART_BACKEND_ROOTPATH") else "http://127.0.0.1/api")
)


async def _find_first_superuser(user_repo: UserRepository) -> Any:
    """Return the oldest superuser, or None if no admin exists yet."""
    stmt = (
        select(models.User)
        .where(models.User.is_superuser.is_(True))
        .order_by(models.User.created)
        .limit(1)
    )
    return (await user_repo.session.execute(stmt)).scalar_one_or_none()


async def _bootstrap_admin_via_http(email: str, password: str) -> None:
    """Create the first admin user via Bitcart's public /users/ endpoint,
    same path the standalone script uses on a fresh install.

    Bitcart allows unauthenticated registration of the *first* user (a
    superuser); subsequent /users/ POSTs require either auth or
    registration-enabled policy. We rely on that first-user exemption —
    which means this only succeeds when truly no users exist yet.

    Returns nothing; the caller refetches the User row via DI to discover
    the new admin's id and bind our app_id-tagged token to them. Going
    through HTTP (rather than the service layer) keeps the bootstrap
    path identical between standalone and plugin modes — same endpoint,
    same validation, same side effects.
    """
    # Late import: this module already has classes.BitcartAPI available
    # in the engine's sys.path once _ensure_engine_importable has run.
    from classes import BitcartAPI

    api = BitcartAPI(BITCART_LOCAL_URL, None)
    token = await api.setup_first_user(email, password)
    if not token:
        raise RuntimeError(
            "liquidityhelper plugin: failed to create admin user via "
            "HTTP. Inspect the API logs for the underlying 4xx/5xx."
        )
    # We discard `token` — it's a generic login token. Our caller
    # creates a fresh token tagged with our plugin's app_id so future
    # restarts find it deterministically.


async def _get_or_create_plugin_token(container: Any, settings: Any) -> str:
    """Find or create a Token row tagged with our app_id and return its id
    (which IS the bearer token string).

    Three paths, in order:
      1. Reuse: a token with our app_id already exists → return its id.
      2. Bind to existing superuser: a superuser exists but no plugin
         token yet → create one for them.
      3. Bootstrap: no superuser exists. If the operator set
         ADMIN_EMAIL+ADMIN_PASSWORD in the plugin settings, create the
         first superuser via the same HTTP endpoint the standalone
         script uses, then bind to them. Otherwise raise.

    The bootstrap path matches the user's spec: 'admin user can be
    created just like the normal non-plugin script run does'.
    """
    from dishka import Scope

    async with container(scope=Scope.REQUEST) as request_container:
        token_repo: TokenRepository = await request_container.get(TokenRepository)
        user_repo: UserRepository = await request_container.get(UserRepository)

        # Path 1: reuse
        existing = await token_repo.get_one_or_none(app_id=PLUGIN_APP_ID)
        if existing is not None:
            return existing.id

        # Path 2 + 3: need a superuser to bind to
        superuser = await _find_first_superuser(user_repo)
        if superuser is None:
            email = getattr(settings, "ADMIN_EMAIL", None)
            password = getattr(settings, "ADMIN_PASSWORD", None)
            if not email or not password:
                raise RuntimeError(
                    "liquidityhelper plugin: no superuser exists. Set "
                    "ADMIN_EMAIL and ADMIN_PASSWORD in the plugin "
                    "settings to auto-create one, or register an admin "
                    "through Bitcart's normal UI first."
                )
            await _bootstrap_admin_via_http(email, password)
            superuser = await _find_first_superuser(user_repo)
            if superuser is None:
                # HTTP call reported success but the row isn't here — could
                # be a transaction-visibility issue. Bail loudly rather
                # than blunder onwards.
                raise RuntimeError(
                    "liquidityhelper plugin: HTTP bootstrap succeeded "
                    "but no superuser is visible in the DB session."
                )

        token_row = models.Token(
            user_id=superuser.id,
            app_id=PLUGIN_APP_ID,
            permissions=list(PLUGIN_TOKEN_PERMISSIONS),
        )
        await token_repo.add(token_row)
        return token_row.id


class Plugin(BasePlugin):
    name = PLUGIN_NAME

    def __init__(self, path: str) -> None:
        super().__init__(path)
        self._stop_event: asyncio.Event | None = None
        self._loop_task: asyncio.Task[None] | None = None

    def setup_app(self, app: FastAPI) -> None:  # noqa: D401 — Bitcart hook
        # PyCharm remote-debug attach (gunicorn HTTP worker side).
        # No-ops when the launcher hasn't set up debug; otherwise
        # connects this process back to PyCharm's listener so HTTP
        # handlers (dashboard, logs, etc.) can be stepped through.
        # Worker process attaches separately in worker_setup().
        try:
            _ensure_engine_importable()
            from liquidityhelper import (
                _load_debug_env_file as _liq_load_debug_env_file,
                maybe_attach_pycharm_debugger as _liq_maybe_attach_pycharm_debugger,
            )
            _liq_load_debug_env_file()
            _liq_maybe_attach_pycharm_debugger()
        except Exception:
            logger.exception(
                "plugin setup_app: debug-attach helpers failed "
                "(non-fatal — engine continues without debugger)"
            )

        # Mount log-viewer endpoints. The bitcart auth dependency is
        # imported lazily so test code can mount the same router with a
        # stub. Routes:
        #   GET /api/plugins/liquidityhelper/logs/streams
        #   GET /api/plugins/liquidityhelper/logs/{stream}?tail=N
        #   GET /api/plugins/liquidityhelper/settings/schema
        from api.utils.authorization import AuthDependency
        app.include_router(build_router(AuthDependency()))
        app.include_router(_build_schema_router(AuthDependency()))
        app.include_router(build_dashboard_router(AuthDependency()))
        app.include_router(build_debug_router(AuthDependency()))
        app.include_router(build_wallet_debug_router(AuthDependency()))
        app.include_router(build_log_export_router(AuthDependency()))

    async def startup(self) -> None:
        # Registering the schema (built from config.py at import time)
        # gives Bitcart what it needs to render the settings page even
        # before worker_setup has run — worker_setup only fires on
        # worker processes, so HTTP-only processes also need this call.
        self.register_settings(PluginSettings)
        # Stash a reference to this Plugin instance on the
        # settings_bridge module so free functions in the engine
        # (run_tick_loop, compute_health_warnings) can call
        # self.get_plugin_settings() through the bridge without
        # being methods on the Plugin class themselves. This is
        # how refresh_settings_from_bitcart reaches bitcart's
        # in-process plugin_registry from outside the Plugin.
        #
        # CRITICAL: register against BOTH module objects. Python
        # keeps `bitcart_plugin.settings_bridge` (top-level,
        # imported by liquidityhelper.py via its sys.path bootstrap)
        # and `modules.@barebits.liquidityhelper.bitcart_plugin.settings_bridge`
        # (package-relative, what `register_plugin_instance` imported
        # at the top of this file resolves to) as SEPARATE module
        # objects in sys.modules with SEPARATE `_plugin_instance_ref`
        # globals. A setattr on one is invisible to the other.
        # `compute_health_warnings` and `run_tick_loop` import the
        # top-level name, so registering only the package version
        # leaves the engine's refresh path seeing `None` and silently
        # skipping the refresh — the original bug that left stale
        # health warnings live on the dashboard.
        # Mirrors the same dual-prime pattern used below for the
        # config / liquidityhelper / classes settings application.
        register_plugin_instance(self)  # registers on the package-path module
        try:
            from bitcart_plugin.settings_bridge import (
                register_plugin_instance as _toplevel_register,
            )
            _toplevel_register(self)  # also registers on the top-level module
        except ImportError:
            # If the top-level alias hasn't been primed yet (or this
            # is an unusual loader), the package-path registration
            # alone keeps backend-side callers working. Worker tick
            # loop's import happens AFTER this point, so by that
            # time both registrations have run.
            logger.debug(
                "plugin startup: top-level bitcart_plugin alias not importable; "
                "package-path registration is in effect"
            )
        # Settings-changed hook: name format is fixed by plugin_registry,
        # see `set_plugin_settings_dict`.
        self.context.register_hook(
            f"settings_changed:{self.name}", self._on_settings_changed
        )
        # Make sure the engine's loggers also write into the plugin's
        # data dir so the log-viewer endpoints have something to read.
        # Idempotent — safe to call again from a settings reload.
        install_plugin_log_sinks(self.data_dir())

        # Apply settings (especially AUTH_TOKEN) onto the engine's
        # module-level globals. Without this the dashboard and other
        # HTTP endpoints would call `api.get_stores()` with a None
        # auth token, get a 401 back, the BitcartAPI wrapper would
        # swallow it and return None, and the caller would crash on
        # `sorted(None)`. worker_setup applies settings too but only
        # in the Celery-style worker process; gunicorn HTTP workers
        # run startup() but not worker_setup(), so the setattr has
        # to happen here as well.
        try:
            current_settings = await self._load_settings()
            token = await _get_or_create_plugin_token(
                self.context.container, current_settings
            )
            current_settings = current_settings.model_copy(update={"AUTH_TOKEN": token})
            _ensure_engine_importable()
            # Prime BOTH the package-path module and the top-level
            # sys.path-resolved name. The engine's own files
            # (liquidityhelper.py, classes.py, config.py) get imported
            # via the package path when bitcart loads us
            # (`modules.@barebits.liquidityhelper.*`) AND as top-level
            # names (`import config`, `import classes`) because
            # liquidityhelper.py's self-bootstrap puts its directory on
            # sys.path. Python keeps the two as SEPARATE module objects
            # in sys.modules, with separate globals — meaning a setattr
            # on `liquidityhelper` doesn't update
            # `modules.@barebits.liquidityhelper.liquidityhelper`. Both
            # need to be updated, or the dashboard handler (which uses
            # the package-path version via `from ..liquidityhelper import …`)
            # sees AUTH_TOKEN=None even though the standalone-style
            # bridge ran successfully.
            import liquidityhelper  # noqa: F401 — primes the top-level alias
            from . import liquidityhelper as _pkg_engine  # noqa: F401 — primes the package alias
            module_targets = ["config", "liquidityhelper", "classes"]
            pkg = __package__ or ""
            if pkg:
                module_targets += [f"{pkg}.config", f"{pkg}.liquidityhelper", f"{pkg}.classes"]
            apply_settings(current_settings, modules=tuple(module_targets))
        except Exception:
            logger.exception(
                "plugin startup: failed to apply settings to engine globals; "
                "dashboard and debug endpoints will probably 500 until this "
                "is resolved"
            )

    async def shutdown(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()
        if self._loop_task is not None:
            # Allow the in-flight tick to drain. If you yank it mid-tick
            # you can leave half-issued LSP orders, half-paid payouts,
            # etc. — way worse than waiting a few seconds.
            try:
                await asyncio.wait_for(self._loop_task, timeout=120)
            except asyncio.TimeoutError:
                logger.warning(
                    "tick loop didn't exit within 120s — cancelling"
                )
                self._loop_task.cancel()
                try:
                    await self._loop_task
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    logger.debug(f"plugin tick-loop task raised on cancellation: {e}")
        # Drain engine resources before the log listener stops so any
        # errors during teardown still get logged. Three sources of
        # leaks across plugin reloads:
        #   - loopd subprocesses (one per wallet, holding gRPC ports)
        #   - cached LND gRPC channels (FDs + connection pools)
        # If we don't shut these down, the next plugin reload either
        # accumulates duplicates or fails to bind ports.
        try:
            from liquidityhelper import close_all_lnd_connections
            await close_all_lnd_connections()
        except Exception:
            logger.exception("plugin shutdown: close_all_lnd_connections failed")
        try:
            from liquidityhelper import close_shared_tick_api
            await close_shared_tick_api()
        except Exception:
            logger.exception("plugin shutdown: close_shared_tick_api failed")
        try:
            from swap_providers import _LOOPD_MANAGER
            await _LOOPD_MANAGER.stop_all()
        except Exception:
            logger.exception("plugin shutdown: LoopdManager.stop_all() failed")
        # Flush the engine's background log listener so the final
        # records make it to disk before Bitcart tears the process down.
        try:
            from liquidityhelper import stop_log_listener
            stop_log_listener()
        except Exception as e:
            logger.debug(f"plugin shutdown: stop_log_listener best-effort cleanup failed: {e}")

    async def worker_setup(self) -> None:
        _ensure_engine_importable()
        # Worker-side PyCharm remote-debug attach. The tick loop runs
        # in this process; without an attach here, breakpoints inside
        # liquidityhelper.main() / do_cashouts() / etc. would never fire.
        try:
            from liquidityhelper import (
                _load_debug_env_file as _liq_load_debug_env_file,
                maybe_attach_pycharm_debugger as _liq_maybe_attach_pycharm_debugger,
            )
            _liq_load_debug_env_file()
            _liq_maybe_attach_pycharm_debugger()
        except Exception:
            logger.exception(
                "plugin worker_setup: debug-attach helpers failed "
                "(non-fatal — worker continues without debugger)"
            )

        # Apply current settings BEFORE importing the engine. Even though
        # the bridge can setattr post-import, doing this once up-front
        # means the engine's module-level constants (logger thresholds,
        # AUTH_TOKEN, etc.) are correct from the very first call.
        current_settings = await self._load_settings()

        # Get the auth token. Engine reads it from config.AUTH_TOKEN, so
        # we slot it into the settings payload before applying. Pass the
        # full settings object so _get_or_create_plugin_token can use
        # ADMIN_EMAIL/ADMIN_PASSWORD to bootstrap the first admin if
        # the install is fresh (no superuser exists yet).
        try:
            token = await _get_or_create_plugin_token(
                self.context.container, current_settings
            )
        except Exception:
            logger.exception("failed to acquire plugin auth token")
            return
        current_settings = current_settings.model_copy(update={"AUTH_TOKEN": token})

        # First-time import of the engine; config.py runs with whatever
        # env-var overrides were already in place. Plugin-bridge then
        # wins over both.
        import liquidityhelper  # noqa: F401 — triggers config import

        apply_settings(current_settings)

        # Spawn the tick loop. Bitcart owns the asyncio loop; we just
        # add a task. Attach a done-callback so that if the task ends
        # with an uncaught exception (which run_tick_loop's outer
        # try/except should prevent, but defense-in-depth costs
        # nothing) we restart it. Without the callback an unobserved
        # task exception would silently end the plugin's tick
        # processing with only a stderr "Task exception was never
        # retrieved" message at process shutdown.
        self._stop_event = asyncio.Event()
        self._loop_task = asyncio.create_task(
            liquidityhelper.run_tick_loop(stop_event=self._stop_event),
            name=f"{self.name}.tick_loop",
        )
        self._loop_task.add_done_callback(self._on_tick_loop_done)

    def _on_tick_loop_done(self, task: "asyncio.Task[None]") -> None:
        """Restart the tick loop if it ended with an exception.
        Cancellation (cooperative shutdown) and clean exits
        (SINGLE_RUN, stop_event) are honored — only true failures
        trigger a respawn."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc is None:
            # Loop returned normally (SINGLE_RUN=True or stop_event
            # set). Don't respawn.
            return
        # Honor an active shutdown signal — if we're tearing down,
        # don't fight it.
        if self._stop_event is not None and self._stop_event.is_set():
            return
        logger.error(
            "tick loop task ended unexpectedly with exception "
            "(run_tick_loop's outer handler should have caught this); "
            "respawning",
            exc_info=exc,
        )
        # Lazy import to avoid pulling the engine into module load.
        import liquidityhelper as _lh
        self._loop_task = asyncio.create_task(
            _lh.run_tick_loop(stop_event=self._stop_event),
            name=f"{self.name}.tick_loop",
        )
        self._loop_task.add_done_callback(self._on_tick_loop_done)

    async def _load_settings(self) -> PluginSettings:
        """Read currently-stored plugin settings, fill in any missing keys
        from config.py defaults."""
        stored = await self.get_plugin_settings()
        stored_dict = stored.model_dump() if stored is not None else {}
        return merge_with_config(stored_dict)

    async def _on_settings_changed(self, new_settings: PluginSettings) -> None:
        """Bitcart calls this when the admin saves the settings form.

        We re-merge with config defaults (in case the user cleared a
        field, we want to fall back to the code default, not None), then
        push everything onto the engine's module namespaces. The next
        tick picks up the new values.
        """
        merged = merge_with_config(new_settings.model_dump())
        # Preserve the auth token through reload — it's not a user-facing
        # setting in plugin mode. If the user did set it in the UI for
        # some reason, that wins.
        if not merged.AUTH_TOKEN:
            try:
                token = await _get_or_create_plugin_token(
                    self.context.container, merged
                )
                merged = merged.model_copy(update={"AUTH_TOKEN": token})
            except Exception:
                logger.exception(
                    "could not refresh plugin token during settings reload"
                )
        # Read DEBUG_MODE BEFORE applying so we can detect a real
        # transition. Apply, then read again. Only on a True → False
        # transition do we fire the trigger to unblock the tick loop.
        #
        # Why this specificity matters: Bitcart's plugin framework may
        # fire `settings_changed:liquidityhelper` at startup as part of
        # initial settings registration, with whatever values are
        # already saved. If DEBUG_MODE was saved as True, the operator
        # explicitly wants the loop parked — we MUST NOT fire the
        # trigger here just because a settings event happened. Firing
        # only on True → False means saves are inert unless they
        # actually leave debug mode, which is exactly the contract.
        try:
            import liquidityhelper as _engine
            debug_was_on = bool(getattr(_engine, "DEBUG_MODE", False))
        except Exception as e:
            logger.debug(f"_on_settings_changed: pre-apply DEBUG_MODE read failed: {e}")
            debug_was_on = False

        applied = apply_settings(merged)
        logger.info(
            "liquidityhelper: applied %d updated settings from admin UI",
            len(applied),
        )

        try:
            import liquidityhelper as _engine
            debug_is_on = bool(getattr(_engine, "DEBUG_MODE", False))
            if debug_was_on and not debug_is_on:
                # True → False: operator left debug mode. Fire the
                # trigger so the parked loop wakes, sees DEBUG_MODE
                # is now False, and resumes continuous operation
                # without requiring an additional manual click.
                _engine.trigger_debug_run_once()
        except Exception:
            # Engine might not be importable during early startup —
            # don't fail the settings apply over a debug-mode nicety.
            pass
