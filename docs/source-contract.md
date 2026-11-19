# 场景目录来源协议

GET /facets 返回单位版本、合法行业、年份、过滤字段以及 cap=4、page_size=2。GET /catalog?unit=east&industry=medical&year=2026&page=0 返回总量、截断标志、页内条目、next 与查询版本；之后可携 version 查询。分页从0开始，total是本范围真实量，items只在前cap项内分页，next=false不代表范围无截断。合法筛选仅 unit/industry/year，不支持自造编号前缀。industry之间重叠，编号仅在unit内唯一。无行业筛选表示该单位全目录。
GET /revisions 是来源修订通知。条目的 status=withdrawn 表示明确撤回，分类退出不代表撤回；完整目录中的缺席和欠采范围的未见不能混用。409 version_expired 应与429单位配额区分，Retry-After单位是秒。
本地控制事件 move/withdraw/remove/backdate/expire 可指定 unit 和 id；throttle 可指定 unit、seconds。move只改该条行业，backdate补入2024年旧需求，expire只改变单位版本使旧查询失效。来源样本特意含同编号不同单位、交叉分类、不可再分仍截断的范围。

本地来源控制通过 POST /__control 发送JSON，例如 {"event":"reset"} 可重置样本。控制切换只影响合成来源，不能用来替业务处理方决定批准、签发或确认。来源服务与自动化客户端分别承担各自责任。
