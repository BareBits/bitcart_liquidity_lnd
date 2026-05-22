from typing import List,Dict,Set,Optional,Iterable,Literal
import traceback
import aiosmtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import ssl
import logging
logger = logging.getLogger(__name__)
class NotificationProvider:
    """
    Base class for a Notification Provider
    """

    def __init__(self, name: str):
        """
        Initialize the Notification Provider
        """
        self.name = name
    async def notify(self, body:str,subject:Optional[str]) -> bool:
        """
        Send a notification, return True if successful, false otherwise
        """
        return False
class EmailNotificationProvider(NotificationProvider):
    def __init__(self,name:str,from_name:str,from_email:str,username:str,password:str,smtp_server:str,smtp_port:int,ssl_enabled:bool,tls_enabled:bool,to_email:str):
        self.name=name
        self.from_name=from_name
        self.from_email=from_email
        self.username=username
        self.password=password
        self.smtp_server=smtp_server
        self.smtp_port=smtp_port
        self.ssl_enabled=ssl_enabled
        self.tls_enabled=tls_enabled
        self.to_email=to_email
    async def test_connection(self)->bool:
        smtp_host=self.smtp_server
        smtp_port=self.smtp_port
        timeout=30
        if self.ssl_enabled == 'ssl':
            context = ssl.create_default_context()
            server = aiosmtplib.SMTP(
                hostname=smtp_host, port=smtp_port,
                timeout=timeout, use_tls=True, tls_context=context,
            )
        else:
            server = aiosmtplib.SMTP(
                hostname=smtp_host, port=smtp_port,
                timeout=timeout, use_tls=False,
            )
        await server.connect()
        try:
            if self.tls_enabled and self.ssl_enabled != 'ssl':
                context = ssl.create_default_context()
                await server.starttls(tls_context=context)
            if self.username and self.password:
                await server.login(self.username, self.password)
        finally:
            try:
                await server.quit()
            except Exception:
                pass
        return True

    async def send_email(
            self,
            subject: str,
            body: str,
            dest_email: Optional[str]=None,
            from_email: Optional[str]=None,
            from_name: Optional[str]=None,
            smtp_host: Optional[str]=None,
            smtp_port: Optional[int]=None,
            username: Optional[str] = None,
            password: Optional[str] = None,
            security: Optional[Literal['none', 'ssl', 'starttls']] = 'starttls',
            timeout: int = 30,
            body_type: Literal['plain', 'html'] = 'plain'
    ) -> bool:
        """
        Send an email via SMTP — async so the up-to-30s TCP+TLS
        handshake never blocks the event loop (was sync `smtplib`,
        which froze the Bitcart worker whenever the SMTP server
        was flaky).

        Args:
            dest_email: Recipient email address
            from_email: Sender email address
            subject: Email subject line
            body: Email body content
            smtp_host: SMTP server hostname
            smtp_port: SMTP server port
            username: SMTP authentication username (optional)
            password: SMTP authentication password (optional)
            security: Security protocol - 'none', 'ssl', or 'starttls'
            timeout: Connection timeout in seconds
            body_type: Email body format - 'plain' or 'html'

        Returns:
            bool: True if email sent successfully, False otherwise
        """
        if not from_email:
            from_email=self.from_email
        if not from_name:
            from_name=self.from_name
        if not dest_email:
            dest_email=self.to_email
        if not smtp_host:
            smtp_host=self.smtp_server
        if not smtp_port:
            smtp_port=self.smtp_port
        if not username:
            username=self.username
        if not password:
            password=self.password
        try:
            msg = MIMEMultipart()
            msg['From'] = f"{from_name} <{from_email}>"
            msg['To'] = dest_email
            msg['Subject'] = subject
            msg.attach(MIMEText(body, body_type))

            if security == 'ssl':
                context = ssl.create_default_context()
                server = aiosmtplib.SMTP(
                    hostname=smtp_host, port=smtp_port,
                    timeout=timeout, use_tls=True, tls_context=context,
                )
            else:
                server = aiosmtplib.SMTP(
                    hostname=smtp_host, port=smtp_port,
                    timeout=timeout, use_tls=False,
                )
            await server.connect()
            try:
                if security == 'starttls':
                    context = ssl.create_default_context()
                    await server.starttls(tls_context=context)
                if username and password:
                    await server.login(username, password)
                await server.send_message(msg)
            finally:
                try:
                    await server.quit()
                except Exception:
                    pass
            return True

        except aiosmtplib.SMTPAuthenticationError as e:
            logger.error(f"Authentication failed: {e}")
            return False

        except aiosmtplib.SMTPConnectError as e:
            logger.error(f"Failed to connect to SMTP server: {e}")
            return False

        except aiosmtplib.SMTPServerDisconnected as e:
            logger.error(f"Server disconnected unexpectedly: {e}")
            return False

        except aiosmtplib.SMTPRecipientsRefused as e:
            logger.error(f"Recipient address refused: {e}")
            return False

        except aiosmtplib.SMTPSenderRefused as e:
            logger.error(f"Sender address refused: {e}")
            return False

        except aiosmtplib.SMTPDataError as e:
            logger.error(f"SMTP data error: {e}")
            return False

        except aiosmtplib.SMTPHeloError as e:
            logger.error(f"SMTP HELO error: {e}")
            return False

        except aiosmtplib.SMTPException as e:
            logger.error(f"SMTP error occurred: {e}")
            return False

        except ssl.SSLError as e:
            logger.error(f"SSL/TLS error: {e}")
            return False

        except TimeoutError as e:
            logger.error(f"Connection timed out: {e}")
            return False

        except OSError as e:
            logger.error(f"Network error: {e}")
            return False

        except Exception as e:
            logger.error(f"Unexpected error: {e} ")
            traceback.print_exc()
            return False
    async def notify(self,
               subject: str,
               body: str,
               ):
        await self.send_email(subject=subject,body=body)
