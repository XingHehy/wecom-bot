"""企业微信消息加解密模块"""
import base64
import random
import string
import hashlib
from Crypto.Cipher import AES
from typing import Tuple
from app.config.settings import CONFIG

def get_aes_key(encoding_aes_key: str) -> bytes:
    """获取AES密钥（32字节）"""
    return base64.b64decode(encoding_aes_key + "=")

# 不再全局生成 aes_key

def create_encrypt_cipher(encoding_aes_key: str) -> AES:
    """创建用于加密的AES实例"""
    aes_key = get_aes_key(encoding_aes_key)
    return AES.new(aes_key, AES.MODE_CBC, aes_key[:16])

def create_decrypt_cipher(encoding_aes_key: str) -> AES:
    """创建用于解密的AES实例"""
    aes_key = get_aes_key(encoding_aes_key)
    return AES.new(aes_key, AES.MODE_CBC, aes_key[:16])

def decrypt(encrypted_data: str, encoding_aes_key: str) -> Tuple[str, str]:
    """解密企业微信加密数据"""
    try:
        cipher = create_decrypt_cipher(encoding_aes_key)
        ciphertext = base64.b64decode(encrypted_data)
        plaintext = cipher.decrypt(ciphertext)
        # 去除PKCS#7填充
        pad = plaintext[-1]
        plaintext = plaintext[:-pad]
        # 解析格式：16字节随机数 + 4字节消息长度（网络字节序） + 消息内容 + 企业ID
        msg_len = int.from_bytes(plaintext[16:20], byteorder='big')
        msg_content = plaintext[20:20 + msg_len].decode('utf-8')
        corp_id = plaintext[20 + msg_len:].decode('utf-8')
        return msg_content, corp_id
    except Exception as e:
        raise ValueError(f"解密失败: {str(e)}")

def encrypt(plaintext: str, encoding_aes_key: str) -> str:
    """加密消息内容，用于被动回复"""
    try:
        cipher = create_encrypt_cipher(encoding_aes_key)
        # 生成16字节随机字符串
        random_str = ''.join(random.choices(string.ascii_letters + string.digits, k=16))
        # 消息格式：随机字符串 + 4字节消息长度（网络字节序） + 消息内容 + 企业ID
        msg_bytes = plaintext.encode('utf-8')
        msg_len = len(msg_bytes).to_bytes(4, byteorder='big')
        corp_id_bytes = CONFIG["CORP_ID"].encode('utf-8')
        content = random_str.encode('utf-8') + msg_len + msg_bytes + corp_id_bytes
        # PKCS#7填充
        pad_len = 32 - (len(content) % 32)
        content += pad_len.to_bytes(1, byteorder='big') * pad_len
        # 加密并Base64编码
        ciphertext = cipher.encrypt(content)
        return base64.b64encode(ciphertext).decode('utf-8')
    except Exception as e:
        raise ValueError(f"加密失败: {str(e)}")

def verify_signature(signature: str, timestamp: str, nonce: str, encrypt_str: str, token: str) -> bool:
    """验证签名是否合法"""
    items = [token, timestamp, nonce, encrypt_str]
    items.sort()
    sha1 = hashlib.sha1()
    sha1.update(''.join(items).encode('utf-8'))
    return sha1.hexdigest() == signature

