"""安装预检与运行期共用的公开定义/profile 一致性校验。"""

def check_profile(profile, row, definitions):
    if (profile['prodId'] != row['pid'] or profile['protocolType'] != 'WiFi'
            or any(profile[name] != row[field] for field, name in
                   [('devType', 'deviceTypeId'), ('manu', 'manufacturerId'), ('model', 'deviceModel')])):
        raise ValueError('cloud product metadata differs from verified profile')
    services = {svc['serviceId']: svc for svc in profile['services']}
    for sid, service_type in row['services'].items():
        definition = definitions['services'][sid] if definitions is not None else None
        profile_type = definition['service_type'] if definition else service_type
        if sid not in services or services[sid]['serviceType'] != profile_type:
            raise ValueError('declared cloud service absent from profile: ' + row['pid'] + '/' + sid)
        if definitions is None:
            continue
        if service_type != definition.get('cloud_service_type', profile_type):
            raise ValueError('undeclared cloud service alias')
        fields = {field['characteristicName']: field for field in services[sid]['characteristics']}
        for name, schema in definitions['services'][sid]['characteristics'].items():
            actual = fields.get(name)
            if (actual is None or actual['characteristicType'] != schema['type']
                    or not set(schema['method']) <= set(actual['method'])):
                raise ValueError('profile characteristic schema differs from mapping')
            for bound in ('min', 'max'):
                if bound in schema and float(actual[bound]) != schema[bound]:
                    raise ValueError('profile characteristic range differs from mapping')
            if 'values' in schema and set(schema['values']) != {int(v['enumVal']) for v in actual['enumList']}:
                raise ValueError('profile characteristic enum differs from mapping')
