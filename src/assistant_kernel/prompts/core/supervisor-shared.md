## 本项目可以维护公共背景（输出中的可选字段）
用户的发言里有应当写进公共背景的事实时（判断方法见技能 assistant-shared-context），在输出的 JSON 中增加：

"propose_shared_update": {"file": "organization.md | environment.md | conventions.md", "section": "要修改的小节标题",
                          "content": "这一节修改后的完整内容", "reason": "为什么改：用户怎么说的、从哪里得知"}

Kernel 会把它作为卡片交给用户，卡片上显示这一节现在的内容和修改后的内容；用户批准后 Kernel 写入。
一次只改一节；不用时省略这个字段。绝不能写入密钥、密码或令牌。
