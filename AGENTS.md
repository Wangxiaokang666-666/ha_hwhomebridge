# AGENTS.md — hwhomebridge

## What This Is

A Home Assistant custom integration that bridges devices into the Huawei HiLink ecosystem. Users control devices through the Huawei Smart Life app or Huawei voice assistant.

**Architecture Evolution**: Originally designed for Xiaomi/mi devices (via the `xiaomi_miot` integration), now refactored to support **any HA device** through generic discovery mechanisms.

The architecture is **Python (business logic) + C (HiLink SDK via ctypes FFI)**, running as a HA custom component in `custom_components/hwhomebridge/`.

## Architecture Overview

```
┌──────────────────────────────────────────────────────┐
│                   HA Python Process                   │
│                                                       │
│  ┌─────────────┐  ┌─────────────┐  ┌──────────────┐ │
│  │ Product      │  │ Product     │  │ Virtual       │ │
│  │ Registry     │─▶│ Matcher     │─▶│ Device        │ │
│  │ (框架+适配器) │  │ (三级匹配)   │  │ (聚合单元)    │ │
│  └─────────────┘  └─────────────┘  └──────┬───────┘ │
│                                             │          │
│                 ┌─────────────┐              │          │
│                 │ Service     │              │          │
│                 │ Router      │◀─────────────┘          │
│                 │ (控制路由)   │                         │
│                 └──────┬──────┘                          │
│                        │                                 │
│  ┌─────────────────────┴───────────────────────┐       │
│  │                hwbridge.py                    │       │
│  └────────────────────┬─────────────────────────┘       │
│                       │ ctypes                           │
└───────────────────────┼─────────────────────────────┘
                        │
┌───────────────────────┼─────────────────────────────┐
│                       ▼                              │
│          libhilink_bridge.so (C)                     │
│          HiLink SDK → 华为云                          │
└─────────────────────────────────────────────────────┘
```

## Three-Layer Configuration Architecture

The configuration is split into three layers, separating stable Huawei definitions from volatile HA mappings:

```
config/
├── product_registry.json              # Layer 1: Framework (华为侧定义, 稳定)
├── adapters/
│   ├── default/                        # Layer 2: Default adapters (华为标准 HA 映射)
│   │   ├── 001.json  002.json  ...   #   从华为 profile 提取的标准映射
│   └── xiaomi/ linp/ zhimi/            # Layer 3: Vendor adapters (厂家差异覆盖)
│       lemesh/ chuangmi/ midea/       #   match_rules + disable_services + 差异覆盖
```

### Layer 1: Framework (`product_registry.json`)

Pure Huawei-side definitions. Contains only:
- `pid`, `name`, `services`
- `services`: `service_type` + `char_name` (mirrors Huawei profile)
- `auto_match`: optional auto-match rules for standard categories (lights, switches, sensors)

**This file never changes** unless a new PID is registered with Huawei.

### Layer 2: Default Adapters (`adapters/default/*.json`)

Standard HA mappings extracted from Huawei profiles. Represents "how a Huawei device would map to HA". Contains:
- `domain`, `action`, `value_attr` (HA protocol-level mapping)
- `value_mapping` (standard enum/text mappings from profile enumList)
- `brightness_range`, `colorTemperature_range`, `colorTemperature_min` (from profile min/max)
- No `match_rules` (not matched directly, only inherited)
- No `name_keywords`, `on_command`, `stop_option` (vendor-specific)
- `exclude_keywords`, `include_keywords` (entity filtering for multi-entity devices)

Used by `auto_match` when no vendor adapter matches. Can be updated independently of framework.

### Layer 3: Vendor Adapters (`adapters/<vendor>/*.json`)

Vendor-specific overrides. Contains:
- `match_rules`: device identification (model_exact, model_keyword, name_keyword)
- `services`: only fields that differ from default (field-level override)
- `disable_services`: framework `service_id` values this device does not have; they are
  removed from the merged `ProductDef` (see below)

**Simple categories** (lights, switches): often only need `match_rules`, inheriting everything from default.

**Complex categories** (cookers, kettles): may override entire services (different domain, value_mapping, etc.).

### The C Side Is a Closed Prebuilt Library

`hilink_bridge/lib/<arch>/libhilink_bridge.so` is a **prebuilt binary**. The repository
contains no `.c`/`.h` sources and no build script; the CMake instructions under
"Build and Deploy" describe a source tree that is not published here.

The library embeds a **fixed device-type table** in `.rodata`. Its complete service
vocabulary is:

```
switch  brightness  cct  status  alarmBell  mode  heatingTarget  temperature
timer   cooker      electric  delay
```

There is **no** `climate`, `cover`, or `fan` anywhere in the binary. The table holds
**17 entries**. Python addresses it by array index via `hwbridge.py:_get_pid_index()`,
which maps only the first 8 positions (`001`–`008`); the remaining 9 are empty
placeholders. That array length is what tells the C side how many types to report, and
the peer side expects a fixed count, so exposing more types is not a Python-only edit.

**Consequence:** a device whose only controllable aspects are `climate`, `cover`, or
`fan` **cannot** be bridged by editing this repository. Supporting one requires editing
the C source, registering a new Huawei PID, and rebuilding the `.so`.

Practical procedure when asked to "support all devices of integration X":

```
1. Enumerate the entity domains owned by each device.
2. If a controllable domain is climate/cover/fan   → blocked on the C side.
3. Otherwise map it onto one of the 8 existing product shapes, adding a vendor
   adapter plus `disable_services` for whatever that shape has but the device lacks.
```

**Known blocked devices** (observed 2026-10-07): 3 air conditioners
(2 × Midea AC LAN `climate`, 1 × `xiaomi.aircondition.mt8`), 4 curtains
(`linp.curtain.ec1db`, `cover`), 2 fans (`dmaker.fan.p45`, `fan`).

### Vendor Adaptors May Disable Framework Services

A vendor adapter may declare `disable_services`, a list of framework `service_id`
values the device does not have. Those services are dropped from the merged
`ProductDef`, so neither the C side nor the entity binder ever sees them.

This is required when a device matches a product shape only partially. Without it the
framework service still finds *some* entity of the matching domain and binds to the
wrong one silently — the concrete failure observed in the field was a light product's
`brightness` / `cct` binding to a switch panel's indicator LED
(`light.*_s_8_indicator_light`), so the panel's actual key was uncontrollable.

`disable_services` applies only when a vendor adapter matched; `auto_match` devices keep
the full framework service set, so ordinary lights still get `brightness` / `cct`.

### Three-Layer Merge

```
framework (service_type, char_name)        ← Huawei definition, never changes
  + default adapter (standard ha_mapping)    ← from Huawei profile
    + vendor adapter (override differences)  ← only fields that differ
      = final ProductDef
```

Merge rule: vendor adapter fields override default (non-null fields win). Framework provides `service_type`/`char_name` unchanged.

### Matching Flow (Three-Level)

```
Device enters
  │
  ├─ 1. Vendor adapter match_rules (by rule type priority):
  │     model_exact → model_keyword → name_keyword → entity_composition
  │     Match → merge(framework + default + vendor) → return
  │
  ├─ 2. Framework auto_match (standard categories zero-config):
  │     required_domains ⊆ device domains + optional name_keywords
  │     Match → merge(framework + default) → return
  │
  └─ 3. No match → skip device
```

## Module Responsibilities

| File | Role |
|------|------|
| `product_registry.py` | Three-layer config loading (framework + default + vendor), merge logic, data models |
| `product_matcher.py` | Three-level matching engine (vendor match_rules → auto_match → skip) |
| `virtual_device.py` | VirtualDevice aggregation unit, SN management |
| `service_router.py` | Control routing (on_command), state reporting, device registration |
| `service_action.py` | ServiceActionDispatcher — dispatches control commands to HA entities |
| `hwbridge.py` | Core: C library init, callback registration, state change listeners |

### Deleted Legacy Files

| File | Status |
|------|--------|
| `device.py` | Replaced by `product_registry.py` + `service_action.py` |
| `light.py` | Replaced by `service_action.py` |
| `fan.py` | Replaced by `service_action.py` |

### Config Files

| File | Role |
|------|------|
| `config/product_registry.json` | Framework: PID, service_type, char_name, auto_match |
| `config/adapters/default/*.json` | Default adapters: standard HA mappings from Huawei profile |
| `config/adapters/<vendor>/*.json` | Vendor adapters: match_rules + difference overrides + disable_services |
| `profile/*.json` | Huawei HiLink device profiles (C-side, **not open source**, gitignored) |

## Key Design Decisions

### 1. VirtualDevice = Aggregation Unit

A `VirtualDevice` represents one Huawei-side logical device (one SN). It aggregates multiple HA entities from the same HA device. One `VirtualDevice` = one SN = one Huawei PID product.

Example: A kettle with switch + temperature sensor → one VirtualDevice with two services (switch, temperature).

### 2. SN Management: From 1:1 to 1:N

- **Before**: SN = entity_id's last 24 chars → SN : entity = 1:1
- **After**: SN = hash(device_id) → SN : VirtualDevice = 1:1, VirtualDevice contains N entities

### 3. Three-Level Product Matching

Vendor adapters are matched by rule type priority:
1. `ModelExactMatch` — entity `device_entry.model` exact match
2. `ModelKeywordMatch` — device.model contains keyword
3. `NameKeywordMatch` — device name keyword matching
4. `EntityCompositionMatch` — entity domain composition matching

If no vendor adapter matches, framework `auto_match` provides zero-config fallback for standard categories (lights, switches, sensors).

### 4. Declarative Control Flow (`on_command`)

Control behavior is declared in JSON, not hard-coded in Python. The `on_command` field in `ha_mapping` defines what happens when `switch.on=1`:

```json
"on_command": {
    "type": "trigger_service",
    "service": "cooker",
    "use_default_mode": true
}
```

The generic `_execute_on_command()` in `service_router.py` handles all device types. No device-specific if-else branches in route_action.

### 5. Control Command Routing (Reverse Direction)

```
OnPyActionCB(sn, payload)
  → ServiceRouter.route_action(sn, payload)
    → Check on_command override (if switch.on=1)
    → VirtualDevice → service → entity
      → ServiceActionDispatcher.dispatch(entity, mapping, payload)
```

### 6. State Reporting (Forward Direction)

```
HA state change event
  → ServiceRouter.report_state(sn, entity_id, state)
    → Determine which service the entity maps to
    → Apply value_mapping (text_to_enum, number_to_enum_multi, etc.)
      → Call C library UpdateHAStatus to report
```

## Value Mapping Mechanism

The `value_mapping` mechanism solves value conversion between Huawei HiLink profile and HA device entities.

### Supported Mapping Types

| Type | Direction | Use Case |
|------|-----------|----------|
| `enum_to_number` | HW enum → HA number | Mode enum to temperature value |
| `text_to_enum` | HA text → HW enum | Sensor/select text state to enum |
| `enum_to_text_multi` | HW enum → HA text list | Select option matching (multi-vendor) |
| `number_to_enum_multi` | HA number → HW enum | Sensor numeric state to enum |
| `seconds_to_minutes` | HA seconds → HW minutes | Timer conversion |
| `delay_to_select` | HW delay → HA select option | Timer service to select entity |

### HAMapping Fields

| Field | Layer | Description |
|-------|-------|-------------|
| `domain`, `action`, `value_attr` | default | HA protocol-level mapping |
| `value_mapping` | default/vendor | Value conversion rules |
| `brightness_range`, `colorTemperature_range` | default | Range limits from profile |
| `colorTemperature_min` | default/vendor | Color temp lower bound (Kelvin) |
| `device_class` | default/vendor | HA device_class filter |
| `name_keywords` | vendor | Entity name keywords for precise matching |
| `exclude_keywords` | default/vendor | Entity exclusion keywords (blacklist) |
| `include_keywords` | default/vendor | Entity inclusion keywords (whitelist) |
| `default_mode` | vendor | Default mode for trigger_service |
| `on_command` | vendor | Declarative control override for switch.on=1 |
| `stop_option` | vendor | Select option for switch off (e.g., "停止") |

See `docs/adapter-guide.md` for detailed documentation.

## Adding a New Device — What to Touch

### Scenario 1: New vendor, standard category (e.g., Tuya light)

**Zero config** — if the device has `light` domain entities, `auto_match` will match it to `002` using default HA mapping. No files to create.

If precise model matching is desired, create a vendor adapter:

```json
// config/adapters/tuya/bulb.json
{
    "pid": "002",
    "match_rules": [
        {"type": "model_keyword", "keywords": ["tuya.light"]}
    ]
}
```

Only `match_rules`, services inherited from default. If brightness range differs:

```json
{
    "pid": "002",
    "match_rules": [{"type": "model_keyword", "keywords": ["tuya.light"]}],
    "services": {
        "brightness": {"brightness_range": 255}
    }
}
```

### Scenario 2: New vendor, complex category (e.g., Midea cooker)

Create a vendor adapter with full service overrides for fields that differ from default:

```json
// config/adapters/midea/cooker.json
{
    "pid": "007",
    "match_rules": [
        {"type": "model_keyword", "keywords": ["MB-FB"]}
    ],
    "services": {
        "switch": {
            "domain": "select",
            "action": "turn_on_off",
            "stop_option": "停止",
            "on_command": {"type": "trigger_service", "service": "cooker", "use_default_mode": true}
        },
        "cooker": {
            "value_mapping": {"type": "enum_to_text_multi", "mapping": {"1": ["精华饭"], ...}}
        }
    }
}
```

### Scenario 3: New Huawei category (new PID)

1. **Framework** (`config/product_registry.json`): Add PID + service_type + char_name
2. **Default adapter** (`config/adapters/default/<PID>.json`): Add standard HA mapping
3. **C side**: Add PID to `map_prodIds[]` etc., define `_XXX_SvcInfo`, add profile JSON, rebuild
4. **PID-to-index**: Add mapping in `hwbridge.py:_get_pid_index()`

### When Python code changes are needed

| Scenario | Need code change? |
|----------|------------------|
| New vendor, same category | No — vendor adapter JSON only |
| New model, same vendor/category | No — add match_rules to existing adapter |
| New value_mapping type | Yes — `ValueMapping` class + transform methods |
| New HA domain (e.g., climate) | Yes — `_report_by_type` + `service_action` |
| New on_command type | Yes — `_execute_on_command` |
| New Huawei PID | Framework + C side (C limitation) |

## Critical Constraints and Gotchas

### ctypes Callback Lifetime
The ctypes callback objects (`pActionCB`, `pTypeCheckCB`, `pDevStatusCB`, `pBridgeStatusCB`, `pGetCharStateCB`, `pPINQueryCB`) are stored as module-level globals. They **must not be garbage collected** — if they are, the C library will call freed function pointers and crash the process.

### SN Generation is Deterministic
`SNManager.generate_sn(device_id)` uses SHA256 hash to generate SNs. The same `device_id` always produces the same SN. This is critical for device persistence across restarts.

### PID-to-Index Mapping Must Stay in Sync
The `_get_pid_index()` function in `hwbridge.py` and the C-side `map_prodIds[]` array must be kept in sync. A mismatch causes the wrong device profile to be sent to HiLink.

### Framework is Source of Truth for Huawei Definitions
`config/product_registry.json` (framework) is the source of truth for Huawei-side definitions (PID, service_type, char_name). Default and vendor adapters only provide HA-side mappings.

### switch + status Shared Entity
When `switch` and `status` services map to the same HA entity (e.g., Midea cooker's `select.工作状态`), both services report state independently. The `status` service syncs `switch_state` cache via `_update_hw_bridge_status(sn, "switch", {"on": switch_val})`.

### on_command Replaces Hard-Coded Logic
The `route_action` method in `service_router.py` uses declarative `on_command` from `ha_mapping` instead of device-specific if-else branches. All device types share the same generic `_execute_on_command()` method.

### Saved Device List
`new_device.txt` persists the list of previously-registered device IDs across restarts. When the bridge goes offline, this file and all config files in `hilink_bridge/config/` are deleted.

### Network Interface Selection
The code uses `psutil.net_if_addrs()` to find the local IP address, skipping `hassio`, `docker*`, and loopback interfaces. This may pick the wrong interface on multi-homed systems.

## Build and Deploy

### C Library Build

**Not reproducible in this repository.** `libhilink_bridge.so` is shipped prebuilt per
architecture (`hilink_bridge/lib/amd64/`, `hilink_bridge/lib/aarch64/`); no C sources,
headers, `CMakeLists.txt`, or build script are published here. To rebuild it you need the
C source tree plus the HiLink SDK (`libhilinkdevicesdk.so`, `libhilinkota.so`,
`libmbedtls.so`), which are **not** present either. Treat the binary as an external
dependency: consuming behaviour may be configured, but no new device type can be added.

### Deployment
The entire `hwhomebridge/` directory is deployed to the HA custom components path:
```
<HA config>/custom_components/hwhomebridge/
```

### Runtime Dependencies
- `psutil` — for network interface discovery (Python package)
- `libhilinkdevicesdk.so`, `libhilinkota.so`, `libmbedtls.so` — HiLink SDK libraries (pre-compiled, in `hilink_bridge/lib/`)
- **No specific integration required** — the bridge discovers devices from any HA integration that creates entities

### Configuration Validation
```bash
python validate_config.py
```
Validates framework + default + vendor adapter JSON files for consistency (PID references, action+domain compatibility, match_rules validity).

## Configuration

- `config/product_registry.json` — Framework: PID, service_type, char_name, auto_match
- `config/adapters/default/*.json` — Default adapters: standard HA mappings from Huawei profile
- `config/adapters/<vendor>/*.json` — Vendor adapters: match_rules + difference overrides
- `hilink_bridge/hilink_bridge.cfg` — HiLink bridge config (productId, deviceTypeId, workdir, udpport, productSN)
- `manifest.json` — HA integration metadata (domain: `hwhomebridge`)

## Key Files for Reference

- `hwbridge.py` — Core: C library init, callback registration, state change handling, ServiceRouter integration
- `service_router.py` — Orchestrator: device registration, control routing (on_command), state reporting
- `virtual_device.py` — VirtualDevice (aggregation unit) and SN management
- `product_registry.py` — Three-layer config loading (framework + default + vendor), merge logic
- `product_matcher.py` — Three-level matching engine (vendor match_rules → auto_match → skip)
- `service_action.py` — ServiceActionDispatcher (control command execution)
- `validate_config.py` — Configuration validation tool
- `hilink_bridge/lib/<arch>/libhilink_bridge.so` — **prebuilt** HiLink bridge binary; the
  C side is not in this repository (see "The C Side Is a Closed Prebuilt Library")
