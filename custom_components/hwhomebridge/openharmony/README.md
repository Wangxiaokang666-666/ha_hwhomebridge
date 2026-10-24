# OpenHarmony 可选后端

复用原项目的产品注册表、匹配器、虚拟设备和路由框架，使用独立 C 子进程桥接华为。原 HiLink 库、产品表和灯光映射保持原有职责。实现基线与可复现构建见 [基线说明](../../../docs/openharmony-baseline.md)，共存实现与隔离验证见 [共存说明](../../../docs/openharmony-coexistence.md)，后续迁移阶段见 [融合计划](../../../docs/plans/openharmony-ha-integration-plan.md)。

配置由本目录的产品注册表、默认/厂家适配器、组合卡片和私有实例组成。根 SN 与 card key 决定确定性的子 SN，不能用重命名 key 的方式改变已配对身份。公开 profile 的产品、服务和特征逐项核对固定哈希；私有配置、运行 KV、token 和密钥不随源码分发。

`HomeAssistantTransport` 在工作线程中调用 HA 主循环的本地状态与服务；生产原生后端不需要 REST token。`RESTTransport` 仅保留为实验工具使用的受限适配器，不能用远端状态镜像代替最终原 HA 的原生验证。`unknown/unavailable` 不会伪造为关闭，写入等待稳定回读。

角色绑定保留主灯亮度、色温、灯效、氛围灯和灯光设置，风扇支持六档/自然风、方向及原生定时。是否允许某实体写入由部署策略限定；不会通过请求参数覆盖实体白名单。二、四档被既有米家同步自动化改写的问题按用户要求保留，不修改自动化。

书房 `shhf.light.sflt11` 按用户实测修正方向极性：物理反转开启对应 HA `forward`，关闭对应 HA `reverse`。厂家适配器的 `verticalswing` 与该型号组合卡片的“风扇反转”开关使用相同转换，指令和状态回报双向生效；其他型号继续使用默认方向映射。

C 通过回环接口按 key/SID 路由；HA 管理该实例的进程、端口、日志和停止任务，配对目录与原 HiLink 独立。删除新条目不得执行旧桥接解绑。
