"""用已验证的四键产品呈现风速选择；不改写华为 profile 或父网关。"""
import copy


def apply_layout(registry, product, layout):
    if layout != 'switches' or product.composition not in registry.bundles:
        raise ValueError('unsupported card layout')
    name = product.composition + ':switches'
    if name not in registry.bundles:
        original = copy.deepcopy(registry.bundles[product.composition])
        fans = [card for card in original if card['pid'] == '28BF']
        if len(fans) != 1 or registry.get_product('2151') is None:
            raise ValueError('verified fan and four-key profiles required')
        fan = fans[0]
        base = registry.merge(registry.get_product('28BF'))
        mapping = registry._merge_ha_mapping(base.services['fan'].ha_mapping,
                                            fan.get('services', {}).get('fan', {}))
        mode = registry._merge_ha_mapping(base.services['mode'].ha_mapping,
                                         fan.get('services', {}).get('mode', {}))
        if mapping.value_mapping is None or mode.value_mapping is None:
            raise ValueError('explicit speed and natural-wind mappings required')
        speeds = sorted(mapping.value_mapping.mapping_dict.items(), key=lambda item: int(item[0]))
        if ([gear for gear, _ in speeds] != [str(gear) for gear in range(1, 7)]
                or mapping.domain != 'fan' or mode.value_attr != 'percentage'):
            raise ValueError('this layout requires six explicit gears and a percentage mode')
        choices = [(f'风速 {gear} 档', percentage, 'set_speed') for gear, percentage in speeds]
        natural = mode.value_mapping.mapping_dict.get('2')
        if natural is None:
            raise ValueError('explicit natural-wind mapping required')
        choices.append(('自然风', natural, 'set_fan_mode'))
        replacements = []
        for index in range(2):
            services = {'switch': {'role': mapping.role, 'domain': 'fan',
                'action': 'turn_on_off', 'value_attr': 'state',
                'options': copy.deepcopy(base.services['switch'].ha_mapping.options)}}
            services['switch']['options'].update(fan.get('services', {}).get('switch', {}).get('options', {}))
            for slot in range(4):
                position = index * 4 + slot
                if position < len(choices):
                    label, percentage, action = choices[position]
                    services[f'switch{slot + 1}'] = {'role': mapping.role, 'domain': 'fan',
                        'action': action, 'value_attr': 'percentage',
                        'value_mapping': {'type': 'enum_to_number', 'mapping': {'0': 0, '1': percentage}},
                        'options': {'binary_choice': True, 'unmapped': 0, 'label': label}}
                else:
                    services[f'switch{slot + 1}'] = {**copy.deepcopy(services['switch']),
                        'options': {**services['switch']['options'], 'label': '风扇电源'}}
            replacements.append({'key': 'fan-controls-' + str(index), 'pid': '2151', 'services': services})
        position = original.index(fan)
        original[position:position + 1] = replacements
        registry.bundles[name] = original
    result = copy.deepcopy(product)
    result.composition = name
    return result


def template_notes(cards):
    """显示真实厂商页面的限制，不将本地服务裁剪宣传为隐藏云端按钮。"""
    notes = []
    pids = {card['pid'] for card in cards}
    if '28BF' in pids:
        notes.append('原风扇模板仍显示 7／8 档、摇头等入口；本型号仅支持六档和自然风。')
    if '2NAL' in pids:
        notes.append('完整定时使用厂商风扇灯页面，仍有未映射入口；主灯亮度和色温使用原主灯卡。')
    if '2151' in pids:
        notes.append('四键卡按按钮名称控制；风速卡总开关控制风扇电源。')
    return notes
