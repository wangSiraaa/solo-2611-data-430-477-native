本地确定性服务适配器只模拟源端认证和分页，不需真实凭据。service.Source 是可替换传输边界；目标应保留该边界，让测试注入 401、响应截断和刷新结果丢失。probe.py concurrent 用 Barrier 固定两个请求先读取旧 refresh 再发送；不是靠 sleep 概率触发。页面的 complete 为假代表 body 未完整，rows 只是前缀，不能提交。
