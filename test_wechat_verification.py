#!/usr/bin/env python3
"""
模拟企业微信验证请求
"""

import hashlib
import time
import requests

# 配置（与软件中一致）
TOKEN = "Ofdj9ZqmevdzX6Je"  # 使用当前配置的Token
URL = "https://uncarted-lumberingly-pamelia.ngrok-free.dev/wx"

# 生成验证参数
timestamp = str(int(time.time()))
nonce = "test_nonce_123"
echostr = "test_echo_string"

# 计算签名
items = sorted([TOKEN, timestamp, nonce])
sha1_str = "".join(items)
signature = hashlib.sha1(sha1_str.encode()).hexdigest()

# 构建请求URL
test_url = f"{URL}?msg_signature={signature}&timestamp={timestamp}&nonce={nonce}&echostr={echostr}"

print("=" * 60)
print("模拟企业微信验证请求")
print("=" * 60)
print(f"Token:     {TOKEN}")
print(f"Timestamp: {timestamp}")
print(f"Nonce:     {nonce}")
print(f"Echostr:   {echostr}")
print(f"Signature: {signature}")
print("-" * 60)
print(f"请求URL: {test_url}")
print("-" * 60)

# 发送请求
try:
    response = requests.get(test_url, timeout=10)
    print(f"响应状态码: {response.status_code}")
    print(f"响应内容: {response.text}")
    print("-" * 60)

    if response.status_code == 200 and response.text == echostr:
        print("✅ 验证成功！返回的echostr正确")
    elif response.status_code == 200:
        print(f"⚠️ 状态码正确，但返回内容不对")
        print(f"   期望: {echostr}")
        print(f"   实际: {response.text}")
    else:
        print(f"❌ 验证失败！状态码: {response.status_code}")
except Exception as e:
    print(f"❌ 请求失败: {e}")

print("=" * 60)
