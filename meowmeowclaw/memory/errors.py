"""记忆子系统错误类型."""


class MemoryStoreError(Exception):
    """记忆存储层基础异常; 由上层(ConversationService)决定降级策略."""


class InvalidSessionKeyError(MemoryStoreError, ValueError):
    """会话键非法(空字段、含 ':' 分隔符、版本号非法等)."""


class SessionStoreError(MemoryStoreError):
    """会话存储读写失败(IO/序列化/已关闭等)."""
