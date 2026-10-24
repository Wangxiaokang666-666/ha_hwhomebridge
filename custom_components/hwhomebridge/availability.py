"""只把缺失或 HA 明确不可用的实体判为离线；配置/转换错误继续报错。"""


class SourceUnavailable(ValueError):
    """当前实体没有可读取的状态，不携带旧状态或关闭值。"""


def require_state(state):
    if state is None:
        raise SourceUnavailable('mapped HA entity missing')
    value = state['state'] if isinstance(state, dict) else state.state
    if value in ('unknown', 'unavailable'):
        raise SourceUnavailable('mapped HA entity unavailable')
    return state
