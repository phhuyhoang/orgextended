import os
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
ImageFormat = Literal['svg', 'png', 'gif', 'jpeg', 'tiff', 'netbpm', 'webp']



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
    image_type = imghdr.what('', binary)
    if image_type is None:
        itype = EnhancedIdentification.detect(image)
        if itype is not None:
            image_type = itype[0]
    return image_type


def image_size_of(image: bytes) -> Tuple[Width, Height]:
    """
    Extract the image width, height from its bytes.
    """
    try:
        image_type = image_format_of(image)
        if image_type == 'svg':
            return ImageSizeExtractor.svg(image)
        elif image_type == 'gif':
            return ImageSizeExtractor.gif(image)
        elif image_type == 'png':
            return ImageSizeExtractor.png(image)
        
        image_type = EnhancedIdentification.detect(image)
        if image_type == ImageType.PNG_LEGACY:
            return ImageSizeExtractor.old_png(image)
        elif image_type == ImageType.JPEG_2000s:
            return ImageSizeExtractor.jpeg_2000s(image)
        elif image_type == ImageType.TIFF_BIG:
            return ImageSizeExtractor.btiff(image)
        elif image_type == ImageType.TIFF_LITTLE:
            return ImageSizeExtractor.lbtiff(image)
        elif image_type == ImageType.TIFF:
            try:
                return ImageSizeExtractor.tiff(image)
            except:
                return ImageSizeExtractor.lbtiff(image)
        elif image_type == ImageType.NETBPM:
            return ImageSizeExtractor.netbpm(image)
        elif image_type == ImageType.WEBP:
            return ImageSizeExtractor.webp(image)
        else:
            try:
                return ImageSizeExtractor.jpeg(image)
            except:
                return ImageSizeExtractor.jpegs(image)
    except:
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


class ImageType:
    GIF = ('gif', '')
    PNG = ('png', '')
    PNG_LEGACY = ('png', 'old')
    JPEG = ('jpeg', '')
    JPEG_2000s = ('jpeg', '2000s')
    TIFF = ('tiff', '')
    TIFF_BIG = ('tiff', 'big-edian')
    TIFF_LITTLE = ('tiff', 'little-edian')
    SVG = ('svg', '')
    NETBPM = ('netbpm', '')
    WEBP = ('webp', '')


class EnhancedIdentification:
    @staticmethod
    def detect(image: bytes) -> Tuple[ImageFormat, str]:
        head = image[:31]
        size = len(image)
        if size >= 10 and head[:6] in (b'GIF87a', b'GIF89a'):
            return ImageType.GIF
        elif size >= 24 and head.startswith(b'\211PNG\r\n\032\n') and head[12:16] == b'IHDR':
            return ImageType.PNG
        elif size >= 16 and head.startswith(b'\211PNG\r\n\032\n'):
            return ImageType.PNG_LEGACY
        elif size >= 2 and head.startswith(b'\377\330'):
            return ImageType.JPEG
        elif size >= 12 and head.startswith(b'\x00\x00\x00\x0cjP  \r\n\x87\n'):
            return ImageType.JPEG_2000s
        elif size >= 8 and head.startswith(b"\x4d\x4d\x00\x2a"):
            return ImageType.TIFF_BIG
        elif size >= 8 and head.startswith(b"\x49\x49\x2a\x00"):
            return ImageType.TIFF
        elif size >= 8 and head.startswith(b"\x49\x49\x2b\x00"):
            return ImageType.TIFF_LITTLE
        elif size >= 5 and (head.startswith(b'<?xml') or head.startswith(b'<svg')):
            return ImageType.SVG
        elif head[:1] == b"P" and head[1:2] in b"123456":
            return ImageType.NETBPM
        elif head.startswith(b"RIFF") and head[8:12] == b"WEBP":
            return ImageType.WEBP


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
    def old_png(image: bytes) -> Tuple[Width, Height]:
        head = image[:31]
        width, height = struct.unpack(">LL", head[8:16])
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

    @staticmethod
    def jpegs(image: bytes) -> Tuple[Width, Height]:
        io = BytesIO(image)
        io.seek(0) # Read 0xff next
        size = 2
        ftype = 0
        while not 0xc0 <= ftype <= 0xcf or ftype in [0xc4, 0xc8, 0xcc]:
            io.seek(size, 1)
            byte = io.read(1)
            while ord(byte) == 0xff:
                byte = io.read(1)
            ftype = ord(byte)
            size = struct.unpack('>H', io.read(2))[0] - 2
        # We are at a SOFn block
        io.seek(1, 1)  # Skip `precision' byte.
        height, width = struct.unpack('>HH', io.read(4))
        return width, height

    @staticmethod
    def jpeg_2000s(image: bytes) -> Tuple[Width, Height]:
        io = BytesIO(image)
        io.seek(48)
        height, width = struct.unpack('>LL', io.read(8))
        return width, height

    @staticmethod
    def tiff(image: bytes) -> Tuple[Width, Height]:
        head = image[:31]
        io = BytesIO(image)
        offset = struct.unpack('<L', head[4:8])[0]
        width, height = -1, -1
        io.seek(offset)
        ifdsize = struct.unpack("<H", io.read(2))[0]
        for _ in range(ifdsize):
            tag, _datatype, _count, data = struct.unpack("<HHLL", io.read(12))
            if tag == 256:
                width = data
            elif tag == 257:
                height = data
            if width != -1 and height != -1:
                break
        return width, height

    @staticmethod
    def btiff(image: bytes) -> Tuple[Width, Height]:
        head = image[:31]
        io = BytesIO(image)
        offset = struct.unpack('>L', head[4:8])[0]
        width, height = -1, -1
        io.seek(offset)
        ifdsize = struct.unpack(">H", io.read(2))[0]
        for _ in range(ifdsize):
            tag, datatype, _count, data = struct.unpack(">HHLL", io.read(12))
            if tag == 256:
                if datatype == 3:
                    width = int(data / 65536)
                elif datatype == 4:
                    width = data
                else:
                    raise ValueError("Invalid TIFF file: width column data type should be SHORT/LONG.")
            elif tag == 257:
                if datatype == 3:
                    height = int(data / 65536)
                elif datatype == 4:
                    height = data
                else:
                    raise ValueError("Invalid TIFF file: height column data type should be SHORT/LONG.")
            if width != -1 and height != -1:
                break
        return width, height

    @staticmethod
    def lbtiff(image: bytes) -> Tuple[Width, Height]:
        head = image[:31]
        io = BytesIO(image)
        width, height = -1, -1
        bytesize_offset = struct.unpack('<L', head[4:8])[0]
        if bytesize_offset != 8:
            raise ValueError('Invalid BigTIFF file: Expected offset to be 8, found {} instead.'.format(bytesize_offset))
        offset = struct.unpack('<Q', head[8:16])[0]
        io.seek(offset)
        ifdsize = struct.unpack("<Q", io.read(8))[0]
        for _ in range(ifdsize):
            tag, _datatype, _count, data = struct.unpack("<HHQQ", io.read(20))
            if tag == 256:
                width = data
            elif tag == 257:
                height = data
            if width != -1 and height != -1:
                break
        return width, height

    @staticmethod
    def netbpm(image: bytes) -> Tuple[Width, Height]:
        io = BytesIO(image)
        io.seek(2)
        sizes = []
        width, height = -1, -1
        while True:
            next_chr = io.read(1)
            if next_chr.isspace():
                continue
            if next_chr == b"":
                raise ValueError("Invalid Netpbm file")
            if next_chr == b"#":
                io.readline()
                continue
            if not next_chr.isdigit():
                raise ValueError("Invalid character found on Netpbm file")
            size = next_chr
            next_chr = io.read(1)
            while next_chr.isdigit():
                size += next_chr
                next_chr = io.read(1)
            sizes.append(int(size))
            if len(sizes) == 2:
                break
            io.seek(-1, os.SEEK_CUR)
        width, height = sizes
        return width, height

    @staticmethod
    def webp(image: bytes) -> Tuple[Width, Height]:
        head = image[:31]
        width, height = -1, -1
        if head[12:16] == b"VP8 ":
            width, height = struct.unpack("<HH", head[26:30])
        elif head[12:16] == b"VP8X":
            width = struct.unpack("<I", head[24:27] + b"\0")[0]
            height = struct.unpack("<I", head[27:30] + b"\0")[0]
        elif head[12:16] == b"VP8L":
            b = head[21:25]
            width = (((b[1] & 63) << 8) | b[0]) + 1
            height = (((b[3] & 15) << 10) | (b[2] << 2) | ((b[1] & 192) >> 6)) + 1
        else:
            raise ValueError("Unsupported WebP file")
        return width, height