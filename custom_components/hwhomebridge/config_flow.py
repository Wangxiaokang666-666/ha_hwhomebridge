"""Config flow for HuaweiHome Bridge

配置流程：
1. 用户在HA设置中点击"添加集成" → 搜索"hwhomebridge"
2. 点击进入配置页面 → 显示提示页面（无PIN码）
3. 用户勾选确认后点击"提交" → 完成集成配置，启动网关
4. 用户再次点击"配置"按钮 → 显示PIN码 → 在APP上绑定
"""

import asyncio
import logging
import time
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback

from .const import DOMAIN
from .pin_manager import PINManager

_LOGGER = logging.getLogger(__name__)


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for HuaweiHome Bridge.
    
    配置流程处理器：
    - 首次配置：显示提示页面，完成后启动网关
    - 后续配置：通过OptionsFlow显示PIN码
    """
    
    VERSION = 1
    
    async def async_step_user(self, user_input=None):
        """第一步：显示提示页面（不显示PIN码）
        
        用户首次添加集成时，网关还未启动，此时不应显示PIN码。
        用户点击"提交"后，集成配置完成，网关启动。
        用户需要再次点击"配置"按钮才能看到PIN码。
        """
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        
        # 用户点击"提交"，创建配置条目
        if user_input is not None:
            _LOGGER.info("Config flow completed, gateway will start")
            return self.async_create_entry(
                title="Huawei HA Gateway",
                data={}
            )
        
        # 显示提示页面（不显示PIN码，无输入框）
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({}),  # 空表单
            last_step=True,
        )
    
    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """获取选项配置流程（显示PIN码）"""
        return OptionsFlowHandler()


class OptionsFlowHandler(config_entries.OptionsFlow):
    """选项配置流程处理器
    
    已配置后，点击"配置"按钮进入此流程，
    显示PIN码用于绑定设备。
    """
    
    __DEFAULT_TIME_OUT = 300  # 5分钟
    
    def __init__(self):
        self._pin_task: asyncio.Task | None = None
        self._abort_reason = ""
    
    async def async_step_init(self, user_input=None):
        """选项配置入口：显示PIN码"""
        # 生成PIN码
        pin_code = PINManager.generate_pin()
        _LOGGER.info(f"Generated PIN for binding: {pin_code}")
        
        # 启动后台任务等待绑定或超时
        if self._pin_task is None:
            self._pin_task = self.hass.async_create_task(
                self._wait_for_binding(pin_code)
            )
        
        return await self.async_step_show_pin()
    
    async def async_step_show_pin(self, user_input=None):
        """显示PIN码页面（无提交按钮）"""
        # 检查后台任务状态
        if self._pin_task.done():
            # 任务完成，检查是否有异常
            if err := self._pin_task.exception():
                if isinstance(err, TimeoutError):
                    _LOGGER.warning("PIN expired after timeout")
                    self._abort_reason = "pin_expired"
                else:
                    _LOGGER.error(f"Unexpected error: {err}")
                    self._abort_reason = "internal_error"
            else:
                # 绑定成功
                _LOGGER.info("Binding completed successfully")
                self._abort_reason = ""
            
            # 进入下一步（成功或失败）
            return self.async_show_progress_done(next_step_id="finish")
        
        # 显示PIN码（使用progress，无提交按钮）
        time_out = self.__DEFAULT_TIME_OUT // 60  # 转换为分钟
        return self.async_show_progress(
            step_id="show_pin",
            progress_action="wait_for_binding",
            description_placeholders={
                "pin_code": str(PINManager.get_current_pin() or ""),
                "time_out": time_out,
            },
            progress_task=self._pin_task,
        )
    
    async def async_step_finish(self, user_input=None):
        """完成配置"""
        if self._abort_reason:
            # 超时或错误，中止流程
            return self.async_abort(reason=self._abort_reason)
        
        # 绑定成功，创建配置条目
        return self.async_create_entry(title="", data={})
    
    async def _wait_for_binding(self, pin_code: int):
        """后台任务：等待用户在APP上完成绑定"""
        start_time = time.time()
        
        while True:
            await asyncio.sleep(2)
            
            # 检查是否超时
            if time.time() - start_time > self.__DEFAULT_TIME_OUT:
                raise TimeoutError("PIN expired")
            
            # 检查PIN是否仍然有效
            current_pin = PINManager.get_current_pin()
            if current_pin is None:
                # PIN已被清除
                _LOGGER.warning("PIN was cleared during binding")
                raise TimeoutError("PIN expired")