import base64
import struct
import traceback
import xml.etree.ElementTree as ET
from io import BytesIO
from typing import Optional, Tuple

from . import imghdr_py3 as imghdr
from .typecompat import Literal


Width = Optional[int]
Height = Optional[int]
ImageFormat = Literal['svg', 'png', 'gif', 'jpeg']


def image_format_of(image: bytes) -> ImageFormat:
    """
    Find the image format directly from its bytes.
    
    Notice: Built-in imghdr for Python 3.3 sometimes sucks at detecting 
    JPEG image format. So I replaced it with this one:
    https://bugs.python.org/file45325

    Topic: https://bugs.python.org/issue28591
    """
    binary = image
    head = binary[:24]
    if len(head) != 24:
        raise TypeError('Not an image')
    if b'<svg' in head:
        return 'svg'
    return imghdr.what('', binary)


def image_size_of(image: bytes) -> Tuple[Width, Height]:
    """
    Extract the image width, height from its bytes.
    """
    image_type = image_format_of(image)
    if image_type == 'svg':
        return ImageSizeExtractor.svg(image)
    elif image_type == 'gif':
        return ImageSizeExtractor.gif(image)
    elif image_type == 'png':
        return ImageSizeExtractor.png(image)
    elif image_type == 'jpeg':
        return ImageSizeExtractor.jpeg(image)
    else:
        return -1, -1


def image_to_base64(image: bytes) -> str:
    """
    Convert an image to a base64-encoded string
    """
    encode = base64.encodestring if hasattr(base64, 'encodestring') else base64.encodebytes
    return str(encode(image).decode('ascii')).replace('\n', '')


def image_to_string(image: bytes) -> str:
    """
    Convert an image to an embeddable string (best for svg)
    """
    return image.decode('ascii').replace('\n', '')


class ImageSizeExtractor:
    @staticmethod
    def svg(image: bytes) -> Tuple[Width, Height]:
        try:
            tree = ET.parse(BytesIO(image))
            root = tree.getroot()
            width, height = root.attrib.get('width', 100), root.attrib.get('height', 100)
            return width, height
        except Exception as error:
            print(error)
            traceback.print_tb(error.__traceback__)
            return 100, 100

    @staticmethod
    def png(image: bytes) -> Tuple[Width, Height]:
        head = image[:24]
        png_bytes = 0x0d0a1a0a
        check = struct.unpack('>i', head[4:8])[0]
        if check != png_bytes:
            return (0, 0)
        width, height = struct.unpack('>ii', head[16:24])
        return width, height

    @staticmethod
    def gif(image: bytes) -> Tuple[Width, Height]:
        head = image[:24]
        width, height = struct.unpack('<HH', head[6:10])
        return width, height

    @staticmethod
    def jpeg(image: bytes) -> Tuple[Width, Height]:
        """
        https://web.archive.org/web/20131016210645/http://www.64lines.com/jpeg-width-height
        """
        try:
            i = 0
            bin_size = len(image)
            if (image[i:4] != b'\xff\xd8\xff\xe0'):
                raise Exception
            i += 4
            if (image[i+2:i+6] == b'JFIF' and image[i+6] == 0x00):
                block_length = image[i] * 256 + image[i+1]
                while (i < bin_size):
                    i += block_length
                    if (i >= bin_size): raise Exception;
                    if (image[i] != 0xFF): raise Exception;
                    if (image[i+1] == 0xC0):
                        height = image[i+5]*256 + image[i+6];
                        width = image[i+7]*256 + image[i+8];
                        return width, height
                    else:
                        i += 2;
                        block_length = image[i] * 256 + image[i+1]
                raise Exception
            else:
                raise Exception
        except Exception:
            try:
                width, height = ImageSizeExtractor._jpeg(image)
                return width, height
            except Exception:
                return None, None

    @staticmethod
    def _jpeg(image: bytes) -> Tuple[Width, Height]:
        try:
            ftype = 0
            pos = 0
            while not 0xc0 <= ftype <= 0xcf:
                byte = image[pos]
                pos += 1
                while byte == 0xff:
                    byte = image[pos]
                    pos += 1
                ftype = byte
                struct.unpack('>H', image[pos:pos+2])[0] - 2
                pos += 2
            # SOFn block
            pos += 1  # skip precision byte.
            height, width = struct.unpack('>HH', image[pos:pos+4])
            return width, height
        except Exception:
            return None, None