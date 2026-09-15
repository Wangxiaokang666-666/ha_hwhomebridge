"""
PIN码管理模块

负责PIN码的生成、管理和过期检测。
设计原则：
1. 线程安全（使用锁保护共享状态）
2. 单例模式（类变量存储状态）
3. 自动过期（5分钟有效期）
4. 进程重启自动清空（符合临时绑定需求）
"""

import secrets
import time
import logging
import threading
from typing import Optional

_LOGGER = logging.getLogger(__name__)


class PINManager:
    """PIN码生成器和管理器（线程安全单例）
    
    用于生成HA网关绑定的临时PIN码。
    每次配置时生成一个新的8位数字PIN码，有效期5分钟。
    
    使用示例：
        pin = PINManager.generate_pin()
        print(f"Generated PIN: {pin}")
        
        # 校验PIN
        if PINManager.is_valid_pin(user_input):
            print("PIN is valid")
    """
    
    _lock = threading.Lock()
    _current_pin: Optional[int] = None
    _expire_timestamp: Optional[float] = None
    
    PIN_LENGTH = 8
    VALID_DURATION_SECONDS = 300  # 5分钟
    
    @classmethod
    def generate_pin(cls) -> int:
        """生成8位随机安全PIN码（整数类型）
        
        使用 secrets.randbelow 生成密码学安全的随机数，
        范围为10000000到99999999的8位纯数字。
        
        Returns:
            int: 8位纯数字PIN码（例如：12345678）
        """
        with cls._lock:
            min_val = 10 ** (cls.PIN_LENGTH - 1)
            max_val = 10 ** cls.PIN_LENGTH
            pin = secrets.randbelow(max_val - min_val) + min_val
            
            cls._current_pin = pin
            cls._expire_timestamp = time.time() + cls.VALID_DURATION_SECONDS
            
            _LOGGER.info(
                f"Generated new PIN: {pin}, expires in {cls.VALID_DURATION_SECONDS}s"
            )
            
            return pin
    
    @classmethod
    def get_current_pin(cls) -> Optional[int]:
        """获取当前有效的PIN码（供C侧回调查询）
        
        检查PIN码是否存在以及是否过期。
        
        Returns:
            Optional[int]: 
                - 返回8位PIN码整数（未过期）
                - 返回None（无PIN码或已过期）
        """
        with cls._lock:
            if cls._current_pin is None or cls._expire_timestamp is None:
                return None
            
            if time.time() > cls._expire_timestamp:
                _LOGGER.info("PIN has expired")
                cls._current_pin = None
                cls._expire_timestamp = None
                return None
            
            return cls._current_pin
    
    @classmethod
    def is_valid_pin(cls, pin: int) -> bool:
        """校验PIN码是否有效
        
        Args:
            pin: 用户输入的PIN码整数
        
        Returns:
            bool: 
                - True: PIN码正确且未过期
                - False: PIN码错误或已过期
        """
        current_pin = cls.get_current_pin()
        if current_pin is None:
            return False
        
        is_valid = (pin == current_pin)
        
        if is_valid:
            _LOGGER.info("PIN validation successful")
        else:
            _LOGGER.warning(f"PIN validation failed")
        
        return is_valid
    
    @classmethod
    def clear_pin(cls):
        """清除当前PIN码
        
        在绑定成功后可以主动调用，释放资源。
        """
        with cls._lock:
            if cls._current_pin is not None:
                _LOGGER.info(f"Clearing PIN: {cls._current_pin}")
            cls._current_pin = None
            cls._expire_timestamp = None
    
    @classmethod
    def get_remaining_seconds(cls) -> int:
        """获取剩余有效秒数（供UI显示）
        
        Returns:
            int: 剩余秒数，0表示已过期或无PIN码
        """
        with cls._lock:
            if cls._expire_timestamp is None:
                return 0
            remaining = int(cls._expire_timestamp - time.time())
            return max(0, remaining)