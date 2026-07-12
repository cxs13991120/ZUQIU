import mimetypes
import os
import smtplib
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path


ROOT = Path(__file__).resolve().parent
REPORT = ROOT / "web" / "daily-report.png"
SITE_URL = "https://l18381527760-sketch.github.io/sporttery-prediction/"


def main() -> int:
    address = os.environ.get("GMAIL_ADDRESS", "").strip()
    app_password = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "").strip()
    recipient = os.environ.get("REPORT_RECIPIENT", address).strip()
    if not address or not app_password or not recipient:
        raise RuntimeError("缺少Gmail地址、收件人或应用专用密码。")
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

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
        smtp.login(address, app_password)
        smtp.send_message(message)
    print(f"Sent daily report to {recipient}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
