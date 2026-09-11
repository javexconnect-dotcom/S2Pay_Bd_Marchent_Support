# S2Pay

Telegram Bot + Telegram Mini App for a controlled Client → Buyer verification-request workflow.

## Final workflow

1. Admin configures one active Client Group and one active Buyer Group.
2. Client opens the Mini App and submits a project.
3. The project is sent to the active Buyer Group as a request card.
4. While the request is PROCESSING, Buyer can use:
   - Apps
   - OTP
   - Message
   - View
   - Success
   - Failed
5. Apps sends a limited request to the Client for the required screenshot/information.
6. Message lets a Buyer member reply with a problem/clarification for the Client.
7. Success or Failed closes the verification workflow and disables the action buttons.

## Security boundary

S2Pay does NOT collect, relay, or store OTP codes.

This ready-to-use package also does NOT collect or relay account PINs/passwords. Sensitive authentication secrets should be entered only into the official verification system/channel.

The workflow automates the non-secret project information, screenshots, and buyer clarification messages.

## Admin commands

Run these inside Telegram:

- `/setup` — configure current group as Client or Buyer
- `/useclient` — make current configured Client Group active
- `/usebuyer` — make current configured Buyer Group active
- `/config` — show active group IDs
- `/chatid` — show current chat ID
- `/status` — request statistics

Only Telegram user IDs in `ADMIN_IDS` can configure groups or close requests.

## Render

Use:
- Web Service
- Docker
- Branch: main
- Root Directory: blank

Required environment variables:
- BOT_TOKEN
- ADMIN_IDS
- MINI_APP_URL (set after the Render URL exists)

Important: SQLite and local uploads are ephemeral on many hosting setups. For durable production history/attachments, attach persistent storage or migrate to PostgreSQL/object storage.
