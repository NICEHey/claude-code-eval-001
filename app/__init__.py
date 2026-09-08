"""水文监测与洪峰事件识别服务。

模块职责：
- validation: 输入校验（水位数值、带时区时间戳等）
- errors:     业务异常（映射为 4xx JSON 响应）
- database:   SQLite 连接管理与表结构
- events:     纯函数事件状态机
- service:    业务编排（建站、批次原子入库、事件重算、查询）
- server:     HTTP 路由层（标准库 http.server）
"""
