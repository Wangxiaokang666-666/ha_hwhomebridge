# 原网关原生扩展检查

当前结论：**未找到可核验的动态产品注册公开接口，单网关新增风扇的发行门槛未通过。** 这是静态证据的边界，不宣称已经证明库内部绝对不存在未公开机制。

更新：已完成[公开源码与隔离Linux调查](legacy-native-public-source-research.md)。上游及18个fork共24个分支未提供C层；找到同类`honyar`适配C/头文件，但附带SDK全部为ARM32，不能直接重建当前两架构库。Linux动态查询也未解析到四个产品/服务适配回调；并未启动SDK或验证原网关新品类注册。

本地库与上游main（`9033ac0`）对应的两种架构均只导出11个具名接口，详情及SHA256见[机器检查结果](legacy-native-audit.json)。它们包括启动/退出、网关身份、AC、网卡、在线状态、PIN/房间/HA回调、子设备上线及状态上报；没有额外的公开产品/PID/profile/服务目录注册入口。两份库均没有静态符号表。

本地`hilink_bridge/`和上游当前完整文件树只有旧库及启动配置，没有桥接适配C源码或头文件。当前Python类型回调只是返回固定的001–008索引，不能把新PID塞到任意索引并认为旧库已经认识它。

## 重复检查

```sh
python3 -m venv /tmp/hwhomebridge-native-audit
/tmp/hwhomebridge-native-audit/bin/pip install -r tools/requirements-native-audit.txt
/tmp/hwhomebridge-native-audit/bin/python tools/audit_legacy_native.py
/tmp/hwhomebridge-native-audit/bin/python -m unittest discover -s tools -p test_audit_legacy_native.py -v
```

工具只解析ELF头和符号，不导入组件，不调用`ctypes.CDLL`或任何SDK函数，不读取启动配置、profile、密钥、HA备份或配对KV，不导出二进制数据段。它永远不会仅凭符号存在就把实际注册验收标为通过；库版本变化时需要重新审查ABI及真实行为。

## 可发给维护者的技术问题草稿

以下仅准备文本，尚未发Issue、PR或消息：

> 希望在原hwhomebridge的单网关、原身份与原有子设备兼容基础上增加配置驱动风扇支持。现有amd64/aarch64的libhilink_bridge.so只导出11个生命周期、回调和上报接口，未找到动态新增产品/profile/服务目录的公开接口。
>
> 请问是否有可维护的桥接适配C源码（例如说明中提到的hilink_profile_bridge.c）、对应头文件、SDK版本/依赖和两架构构建方式？若支持动态目录注册，请提供函数签名、调用顺序、ABI版本及目录边界。
>
> 希望同时明确：PID/profile和SDK的可发行范围；新PID如何加入原网关；旧001–008索引如何保持；正常停止与解绑如何区分；是否有可使用的本地测试身份/夹具。无需在公开讨论中提供AC Key、PIN或用户配对资料。
>
> 我们可以先提交不启用新产品的公共Python能力与兼容性测试。新的风扇产品将等同一根身份下旧灯光和新风扇同时注册、控制、回读及重启通过后再启用。

## 继续推进的边界

- 可以先完成公共映射、方向/模式、角色和原生定时抽象、兼容性测试及独立的贡献分支。
- 暂不修改原生索引或生产产品目录，不在原HA安装，不继续沿用双网关生产方案。
- 只有取得可验证的动态ABI，或可重建适配源码及必要SDK后，才能继续原网关原生接入。拿不到时，完整OpenHarmony单引擎替换需另行验证全部旧能力及身份，不能默认无损。
