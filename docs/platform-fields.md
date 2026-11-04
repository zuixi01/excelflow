# 平台动态字段接入

模板管理 → 选择模板 → 动态字段 → **对接平台字段**。管理员填写一个返回全部商品属性和商品规格的**字段接口完整地址**（如 `https://api.example.com/fields`），选择 GET/POST 请求方式并按需填写 API 密钥，点击**保存连接**，再点击**连接并预览字段**。每个字段无需单独配置路径。确认内容后导入模板草稿。导入不会自动保存或发布；保存草稿并发布后，新任务才使用这些字段，已有任务继续使用自己的字段快照。

目前尚无实际平台接口信息。页面支持标准的 `Authorization: Bearer <API 密钥>` 鉴权；无鉴权的接口可留空。密钥通过管理员请求发往后端，服务端加密存放，后续读取页面只返回“已保存密钥”，不会回显原文。更换完整地址会清除旧密钥，必须重新填写。生产环境需设置 `EXCEL_AUDITOR_PLATFORM_KEY_FILE` 指向持久、受保护的 Fernet 密钥文件（可用 `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` 生成文件内容）；开发环境 Windows 使用系统账户保护，非 Windows 开发环境在数据目录自动生成密钥文件。地址默认只允许公网 HTTPS；仅在服务器显式设置 `EXCEL_AUDITOR_ALLOW_PRIVATE_PLATFORM=1` 时允许内网地址，HTTP 只在非生产环境设置 `EXCEL_AUDITOR_ALLOW_INSECURE_PLATFORM_HTTP=1` 后允许。本机双击启动脚本为方便开发联调，已启用 HTTP 和内网访问；生产部署不沿用此开发脚本。

服务器受管连接仍可在页面的“使用服务器已有连接”中选用，适合需要预设静态请求参数的场景：

1. 将 `configs/connections.platform-fields.example.json` 复制为数据目录下的 `var/connections.json`，修改 `base_url`、`allowed_paths`、`field_definition.path` 和 `tenant_ids`。若已有连接文件，应将这一项合入原文件，保留其他连接。
2. 将平台访问令牌放在 `var/secrets/product_fields_token`，只授权启动后端的账户读取；不要放进连接 JSON 或前端。若平台接口无需鉴权，可删除 `auth_secret_ref`。本机启动脚本会在下次启动时读取这两个路径。
3. 生产环境由管理员设置 `EXCEL_AUDITOR_CONNECTIONS` 和 `EXCEL_AUDITOR_SECRET_DIR` 指向受保护的连接文件、密钥目录。内网或 HTTP 接口需要管理员在受管连接中显式允许相应网络；默认仅允许公网 HTTPS。

平台接口需返回 JSON 对象，根节点或 `data` 对象包含 `categories` 数组。例如：

```json
{
  "data": {
    "schema_version": "2026-11-04",
    "categories": [
      {
        "id": "lighting",
        "title": "灯具",
        "attributes": [
          {"id": "brand", "title": "品牌", "type": "string", "aliases": ["厂牌"]}
        ],
        "specifications": [
          {"id": "power", "title": "功率", "type": "string"}
        ]
      }
    ]
  }
}
```

字段支持现有模板的 `ConfiguredField` 属性：`id`、`title`、`type`、`required`、`aliases`、`description`、`enum_values` 等。类目最多 19 个，每类商品属性和商品规格各最多 100 个。平台的字段 ID 若有不同命名规则，或需要分页、特定请求参数，应根据实际接口响应增加适配器；不要直接将非标准响应作为模板发布。
