# 企业微信URL验证失败排查方案

## 当前状态
- URL: https://uncarted-lumberingly-pamelia.ngrok-free.dev/wx
- Token: qA4apo9InmbRjXUkKeEpd
- EncodingAESKey: BlKCcua9u3PmF1HfgkRVGrdV5v26vXtop009JekisQE
- 问题：企业微信后台URL一直提示不通过

## 立即执行的排查步骤

### 步骤1：确认ngrok隧道是否正常

在终端执行：
```bash
curl -v "https://uncarted-lumberingly-pamelia.ngrok-free.dev/wx?msg_signature=test&timestamp=123&nonce=abc&echostr=hello"
```

**期望结果：**
- 如果返回403或其他HTTP响应 → 隧道正常
- 如果超时或无法连接 → 隧道有问题

### 步骤2：重新启动软件和服务

1. 关闭软件
2. 重新打开软件
3. 在"自动化"标签页，确认配置：
   - Token: `qA4apo9InmbRjXUkKeEpd`
   - EncodingAESKey: `BlKCcua9u3PmF1HfgkRVGrdV5v26vXtop009JekisQE`
4. 点击"保存配置"
5. 点击"一键启动"
6. 等待看到成功消息

### 步骤3：查看详细日志

在软件日志窗口中，你应该看到：
```
🌐 ngrok 隧道建立成功：https://xxx.ngrok.io
✅ 本地服务已启动，端口 8899
```

### 步骤4：在企业微信后台保存配置

1. 确保URL、Token、EncodingAESKey都正确填写
2. 点击"保存"
3. **立即查看软件日志**

### 步骤5：分析日志输出

**如果看到：**
```
📥 收到验证请求：path=/wx?msg_signature=xxx...
   timestamp=1234567890, nonce=abcdefg...
[INFO] 签名验证: token=qA4apo9InmbRjXUkKeEpd...
[INFO] 计算的签名: abc123...
[INFO] 收到的签名: xyz789...
[INFO] 验证结果: True
✅ 签名验证通过，返回echostr
```
→ 验证成功！如果企业微信还显示失败，可能是返回格式问题

**如果看到：**
```
[INFO] 验证结果: False
❌ 签名验证失败！
```
→ Token配置不一致，检查大小写和空格

**如果什么都没看到：**
→ 企业微信的请求没有到达服务器，检查：
  - ngrok隧道是否正常
  - URL是否正确
  - 网络连接

## 常见问题和解决方案

### 问题1：Token不一致
**症状：** 日志显示"签名验证失败"
**解决：**
1. 在软件中查看Token配置
2. 在企业微信后台查看Token配置
3. 确保完全一致（区分大小写）
4. 重新保存配置

### 问题2：ngrok隧道不稳定
**症状：** 有时能收到请求，有时收不到
**解决：**
1. 检查网络连接
2. 重启ngrok隧道（停止服务再启动）
3. 考虑使用付费版ngrok获得稳定连接

### 问题3：URL路径问题
**症状：** 企业微信显示"URL不可访问"
**解决：**
1. 确认URL格式正确：`https://xxx.ngrok.io/wx`
2. 不要添加多余的路径或参数
3. 确保使用https而不是http

### 问题4：端口被占用
**症状：** 软件显示"本地服务启动失败"
**解决：**
```bash
# 查看8899端口是否被占用
lsof -i :8899

# 如果被占用，杀掉进程
kill -9 <PID>
```

## 使用测试工具

我已经创建了一个测试工具：`test_wechat_signature.py`

使用方法：
```bash
cd /Users/shyn/Desktop/project2
python3 test_wechat_signature.py
```

然后输入从软件日志中获取的参数进行测试。

## 最后的建议

如果以上所有步骤都无法解决问题：

1. **截图软件完整日志**（包括启动到验证失败的全过程）
2. **截图企业微信后台的错误提示**
3. **执行curl测试并截图结果**

把这些信息发给我，我可以精确定位问题！

## 快速检查清单

- [ ] Token配置正确且一致
- [ ] EncodingAESKey配置正确且一致
- [ ] ngrok token已配置
- [ ] 软件显示"隧道建立成功"
- [ ] 软件显示"本地服务已启动"
- [ ] URL格式正确（https://xxx.ngrok.io/wx）
- [ ] 企业微信后台配置与软件一致
- [ ] 查看了软件日志
- [ ] 尝试了curl测试

如果所有项都打勾但还是失败，那就需要看具体的日志内容了。
