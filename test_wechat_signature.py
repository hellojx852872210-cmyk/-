#!/usr/bin/env python3
"""
企业微信签名验证测试工具
用于测试和调试企业微信回调URL验证
"""

import hashlib
import sys

def verify_signature(token, timestamp, nonce, signature):
    """
    验证企业微信签名

    参数:
        token: 在企业微信后台配置的Token
        timestamp: 时间戳
        nonce: 随机数
        signature: 企业微信发送的签名
    """
    # 1. 将token、timestamp、nonce三个参数进行字典序排序
    items = sorted([token, timestamp, nonce])

    # 2. 将三个参数字符串拼接成一个字符串
    sha1_str = "".join(items)

    # 3. 进行sha1加密
    calculated_sig = hashlib.sha1(sha1_str.encode()).hexdigest()

    # 输出调试信息
    print("=" * 60)
    print("企业微信签名验证测试")
    print("=" * 60)
    print(f"Token:     {token}")
    print(f"Timestamp: {timestamp}")
    print(f"Nonce:     {nonce}")
    print(f"排序后:    {items}")
    print(f"拼接字符串: {sha1_str}")
    print("-" * 60)
    print(f"计算的签名: {calculated_sig}")
    print(f"收到的签名: {signature}")
    print("-" * 60)

    if calculated_sig == signature:
        print("✅ 签名验证通过！")
        return True
    else:
        print("❌ 签名验证失败！")
        print("\n可能的原因：")
        print("1. Token配置不一致")
        print("2. 时间戳或随机数获取错误")
        print("3. 字符编码问题")
        return False

def main():
    print("\n请输入企业微信验证请求的参数：\n")

    # 从用户输入获取参数
    token = input("Token (在软件中配置的): ").strip()
    if not token:
        token = "qA4apo9InmbRjXUkKeEpd"  # 默认值
        print(f"使用默认Token: {token}")

    timestamp = input("Timestamp (从企业微信请求中获取): ").strip()
    nonce = input("Nonce (从企业微信请求中获取): ").strip()
    signature = input("Signature (从企业微信请求中获取): ").strip()

    if not all([timestamp, nonce, signature]):
        print("\n❌ 错误：必须提供 timestamp, nonce, signature 参数")
        print("\n提示：这些参数可以从软件日志中获取")
        print("或者从企业微信的验证请求URL中提取")
        return

    # 验证签名
    verify_signature(token, timestamp, nonce, signature)
    print("\n" + "=" * 60)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n已取消")
        sys.exit(0)
