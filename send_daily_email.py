import mimetypes
import os
import smtplib
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path


ROOT = Path(__file__).resolve().parent
REPORT = ROOT / "web" / "daily-report.png"
SITE_URL = "https://cxs13991120.github.io/ZUQIU/"


def main() -> int:
    address = os.environ.get("QQ_ADDRESS", os.environ.get("GMAIL_ADDRESS", "594241761@qq.com")).strip()
    app_password = os.environ.get("QQ_APP_PASSWORD", os.environ.get("GMAIL_APP_PASSWORD", "pgvhqxqientebbjc")).replace(" ", "").strip()
    recipient = os.environ.get("REPORT_RECIPIENT", "594241761@qq.com").strip()
    if not address or not app_password or not recipient:
        raise RuntimeError("缺少邮箱地址、收件人或授权码。")
    if not REPORT.exists() or REPORT.stat().st_size == 0:
        raise RuntimeError(f"日报图片不存在或为空：{REPORT}")

    beijing_now = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=8)))
    message = EmailMessage()
    message["From"] = address
    message["To"] = recipient
    message["Subject"] = f"竞彩足球方案与盈亏日报 {beijing_now:%Y-%m-%d}"
    message.set_content(
        "附件是今天的模拟投注方案和全部盈亏记录。\n\n"
        f"在线网站：{SITE_URL}\n\n"
        "说明：足球比赛存在较大随机性，内容仅用于模拟记录和模型复盘。"
    )
    mime_type, _ = mimetypes.guess_type(REPORT.name)
    main_type, sub_type = (mime_type or "image/png").split("/", 1)
    message.add_attachment(
        REPORT.read_bytes(),
        maintype=main_type,
        subtype=sub_type,
        filename=f"竞彩足球日报-{beijing_now:%Y-%m-%d}.png",
    )

    smtp_server = os.environ.get("SMTP_SERVER", "smtp.qq.com").strip()
    smtp_port = int(os.environ.get("SMTP_PORT", "465"))
    with smtplib.SMTP_SSL(smtp_server, smtp_port, timeout=30) as smtp:
        smtp.login(address, app_password)
        smtp.send_message(message)
    print(f"Sent daily report to {recipient}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
