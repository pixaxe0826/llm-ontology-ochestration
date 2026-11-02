"""Bounded OpenAI image content; URLs are validated without local-network access."""
import base64
import binascii
import http.client
import ipaddress
import json
import socket
import queue
import threading
import time
from urllib.parse import urljoin, urlsplit

from .security import contains_key

MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGES = 4
MAX_REQUEST_BYTES = 30 * 1024 * 1024
# Reserve the entire Muse Spark Open input context, never count base64 as text tokens.
# Actual charges always come from each provider response's usage.
VISION_INPUT_RESERVE = 1_048_576
IMAGE_TYPES = {'image/png', 'image/jpeg', 'image/webp', 'image/gif'}


def image_bytes(data):
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError('Image exceeds the 5 MiB per-image limit.')
    if not (data.startswith(b'\x89PNG\r\n\x1a\n') or data.startswith(b'\xff\xd8\xff')
            or data.startswith((b'GIF87a', b'GIF89a'))
            or data.startswith(b'RIFF') and data[8:12] == b'WEBP'):
        raise ValueError('Image must be PNG, JPEG, WebP or GIF.')
    if contains_key(data.decode('latin1')):
        raise ValueError('Product proxy images must not contain registered secrets.')
    return len(data)


def data_image_size(url):
    header, comma, encoded = url.partition(',')
    if not comma or header.lower() not in {'data:'+kind+';base64' for kind in IMAGE_TYPES}:
        raise ValueError('Image data URI must use PNG, JPEG, WebP or GIF base64.')
    if len(encoded) > 4 * ((MAX_IMAGE_BYTES + 2) // 3):
        raise ValueError('Image exceeds the 5 MiB per-image limit.')
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise ValueError('Invalid base64 image data URI.') from None
    return image_bytes(data)


def public_target(url, deadline=None):
    """Resolve once, reject every private answer, and pin the checked IP on connect."""
    if len(url) > 8192 or contains_key(url) or any(ord(char) <= 32 or ord(char) == 127 for char in url):
        raise ValueError('Invalid public image URL.')
    try:
        parsed = urlsplit(url)
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise ValueError()
        if port != (443 if parsed.scheme == 'https' else 80):
            raise ValueError()
        host = parsed.hostname.encode('idna').decode('ascii')
        remaining=8 if deadline is None else min(8,deadline-time.monotonic())
        if remaining<=0:raise ValueError()
        resolved=queue.Queue(maxsize=1)
        def resolve():
            try:resolved.put(socket.getaddrinfo(host,port,type=socket.SOCK_STREAM))
            except OSError:resolved.put(None)
        threading.Thread(target=resolve,daemon=True).start()
        answers=resolved.get(timeout=remaining)
        if not answers or any(not ipaddress.ip_address(answer[4][0]).is_global for answer in answers):
            raise ValueError()
    except (OSError, ValueError, UnicodeError, queue.Empty):
        raise ValueError('Image URL must resolve only to public HTTP(S) addresses on standard ports.') from None
    return parsed, host, port, answers[0][4][0]


def remote_image_size(url, deadline):
    return len(download_public_image(url,deadline)['data'])


def download_public_image(url, deadline, allowed_types=None):
    """Check at most 5 MiB, validate each redirect, never forward auth/proxy headers."""
    try:
        for redirect in range(4):
            parsed, host, port, address = public_target(url,deadline=deadline)
            remaining = deadline-time.monotonic()
            if remaining <= 0:raise ValueError('Image URL validation timed out.')
            timeout = min(8, remaining)
            connection = (http.client.HTTPSConnection if parsed.scheme == 'https' else http.client.HTTPConnection)(host, port, timeout=timeout)
            # http.client retains the original hostname for TLS SNI and verification.
            connection._create_connection = lambda target, timeout=timeout, source_address=None, **kw: socket.create_connection((address, port), timeout, source_address)
            try:
                path = parsed.path or '/'
                if parsed.query:path += '?'+parsed.query
                connection.request('GET', path, headers={'Accept':'image/*', 'User-Agent':'Muse-Image-Validation/1'})
                response = connection.getresponse()
                if response.status in (301, 302, 303, 307, 308):
                    if redirect == 3 or not response.getheader('Location'):
                        raise ValueError('Too many image URL redirects.')
                    url = urljoin(url, response.getheader('Location'))
                    continue
                if response.status != 200:
                    raise ValueError('Image URL could not be read.')
                content_type=response.getheader('Content-Type', '').split(';')[0].strip().lower()
                if content_type not in (IMAGE_TYPES if allowed_types is None else allowed_types):
                    raise ValueError('Image URL must return PNG, JPEG, WebP or GIF.')
                length = response.getheader('Content-Length')
                if length is not None and int(length) > MAX_IMAGE_BYTES:
                    raise ValueError('Image exceeds the 5 MiB per-image limit.')
                data = bytearray()
                while len(data) <= MAX_IMAGE_BYTES:
                    if time.monotonic() >= deadline:raise ValueError('Image URL validation timed out.')
                    if connection.sock:connection.sock.settimeout(min(8, max(.01, deadline-time.monotonic())))
                    part = response.read(min(65536, MAX_IMAGE_BYTES+1-len(data)))
                    if not part:break
                    data.extend(part)
                data=bytes(data);image_bytes(data)
                actual_type=image_type(data)
                if allowed_types is not None and (actual_type not in allowed_types or actual_type!=content_type):
                    raise ValueError('Image signature does not match an allowed PNG, JPEG or WebP content type.')
                return {'data':data,'url':url,'content_type':actual_type}
            finally:
                connection.close()
    except ValueError as error:
        # Only our own fixed messages may cross the HTTP boundary.
        if str(error).startswith(('Image ', 'Invalid ', 'Too many ', 'Product proxy ')):raise
        raise ValueError('Image URL validation failed.') from None
    except (OSError, http.client.HTTPException, UnicodeError):
        raise ValueError('Image URL validation failed.') from None


def image_type(data):
    image_bytes(data)
    if data.startswith(b'\x89PNG\r\n\x1a\n'):return 'image/png'
    if data.startswith(b'\xff\xd8\xff'):return 'image/jpeg'
    if data.startswith(b'RIFF') and data[8:12]==b'WEBP':return 'image/webp'
    return 'image/gif'


def validate_messages(messages):
    """Validate without rewriting messages, image URLs, detail or other content."""
    if not isinstance(messages, list) or not 1 <= len(messages) <= 80:
        raise ValueError('Provide 1–80 messages.')
    if contains_key(json.dumps(messages, ensure_ascii=False)):
        raise ValueError('Product proxy messages must not contain registered secrets.')
    urls = []
    for message in messages:
        if not isinstance(message, dict):
            raise ValueError('Invalid product proxy message role.')
        from .product_tools import message_tools
        has_calls=message_tools(message)
        content = message.get('content')
        if content is None and has_calls:continue
        if isinstance(content, str):
            if len(content) > 100000:raise ValueError('Message text exceeds 100000 characters.')
            continue
        if not isinstance(content, list) or not 1 <= len(content) <= 64:
            raise ValueError('Message content must be text or a non-empty content array (maximum 64 parts).')
        text_length = 0
        for part in content:
            if not isinstance(part, dict):raise ValueError('Invalid message content part.')
            if part.get('type') == 'text' and isinstance(part.get('text'), str):
                text_length += len(part['text'])
            elif part.get('type') == 'image_url' and message['role'] == 'user':
                image = part.get('image_url')
                if not isinstance(image, dict) or not isinstance(image.get('url'), str) or not image['url']:
                    raise ValueError('image_url requires a non-empty url string.')
                if image.get('detail', 'auto') not in ('auto', 'low', 'high'):
                    raise ValueError('Image detail must be auto, low or high.')
                urls.append(image['url'])
            else:
                raise ValueError('Only text and user image_url content parts are supported.')
        if text_length > 100000:raise ValueError('Message text exceeds 100000 characters.')
    if len(urls) > MAX_IMAGES:raise ValueError('Request exceeds the 4-image limit.')
    # Count first so rejected requests cannot trigger any remote fetch.
    deadline = time.monotonic()+20
    sizes = [data_image_size(url) if url.startswith('data:') else remote_image_size(url, deadline) for url in urls]
    return {'proxy_image_count':len(urls), 'proxy_image_bytes':sum(sizes)}
