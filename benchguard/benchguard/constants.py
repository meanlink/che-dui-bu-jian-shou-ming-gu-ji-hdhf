"""系统常量与枚举。"""

# 匿名门槛：除"从未入选"组外，任一入选组合组少于该件数即拒发。
K_ANONYMITY = 5

# 角色
ROLE_MAINTAINER = "maintainer"   # 机务核验：可看部件名单、可撤下
ROLE_APPLICANT = "applicant"     # 申请单位：可申请、可撤下自己的、看拒发原因

# 申请状态
STATUS_PENDING = "pending"
STATUS_PROCESSING = "processing"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"

# 发布状态
RELEASE_LISTED = "listed"
RELEASE_WITHDRAWN = "withdrawn"

# 审计动作
AUDIT_LOGIN = "login"
AUDIT_LOGOUT = "logout"
AUDIT_APPLY = "apply"
AUDIT_APPROVE = "approve"
AUDIT_REJECT = "reject"
AUDIT_WITHDRAW = "withdraw"
AUDIT_VIEW_ROSTER = "view_roster"

# 幂等键长度
IDEMPOTENCY_MAX_LEN = 80
SERIAL_MAX_LEN = 64
REASON_MAX_LEN = 2000
