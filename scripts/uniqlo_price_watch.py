from __future__ import annotations

import json
import mimetypes
import os
import smtplib
import ssl
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import make_msgid
from html import escape
from pathlib import Path
from typing import Any, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

API_URL = "https://d.uniqlo.cn/p/hmall-sc-service/search/searchWithCategoryCodeAndConditions/zh_CN"
BASE_URL = "https://www.uniqlo.cn"
DETAIL_URL_TEMPLATE = f"{BASE_URL}/product-detail.html?productCode={{product_code}}&searchFlag=true"
SMTP_HOST = "smtp.qq.com"
SMTP_PORT = 465
PAGE_SIZE = 20
PRICE_THRESHOLD = 59.0
TARGET_SIZE_CODE = "SMA005"
ROOT_DIR = Path(__file__).resolve().parent.parent
STATE_PATH = ROOT_DIR / "state" / "uniqlo_ut_l_alerted.json"
MAIL_FROM_ENV = "UNIQLO_MAIL_FROM"
MAIL_TO_ENV = "UNIQLO_MAIL_TO"
MAIL_AUTH_CODE_ENV = "UNIQLO_QQ_SMTP_AUTH_CODE"
REQUEST_HEADERS = {
    "Accept": "application/json",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
    "Content-Type": "application/json",
    "Origin": BASE_URL,
    "Pragma": "no-cache",
    "Referer": f"{BASE_URL}/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
    ),
}

type JsonDict = dict[str, Any]
type PageFetcher = Callable[[int], tuple[list[JsonDict], int]]
type ImageFetcher = Callable[[str], bytes | None]
type MessageSender = Callable[[EmailMessage, MailConfig], None]


@dataclass(frozen=True, slots=True)
class Product:
    """标准化后的商品信息。"""

    code: str
    product_code: str
    name: str
    min_price: float
    origin_price: float
    image_url: str
    detail_url: str
    style_texts: tuple[str, ...]
    size_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AlertRecord:
    """已提醒商品的持久化记录。"""

    product_code: str
    name: str
    alerted_price: float
    alerted_at: str


@dataclass(frozen=True, slots=True)
class InlineImage:
    """邮件中的内联图片。"""

    cid: str
    data: bytes
    mime_type: str


@dataclass(frozen=True, slots=True)
class MailConfig:
    """邮件发送配置。"""

    mail_from: str
    mail_to: str
    smtp_auth_code: str


def build_request_payload(page: int) -> JsonDict:
    """构造固定筛选条件的接口请求体。

    Args:
        page: 当前页码。

    Returns:
        可直接序列化为 JSON 的请求体。
    """

    return {
        "url": (
            "/c/utyinhua_m.html?exist=%5B%7B%22title%22%3A%22%E5%B0%BA%E7%A0%81%22%2C"
            "%22items%22%3A%5B%7B%22sizeValue%22%3A%22L%22%2C%22sizeCode%22%3A%22SMA005"
            "%22%2C%22sizeNoSuffix%22%3A%22005%22%7D%5D%7D%5D&lineUpCode=&rank=priceAsc"
        ),
        "pageInfo": {"page": page, "pageSize": PAGE_SIZE, "withSideBar": "Y"},
        "belongTo": "pc",
        "rank": "priceAsc",
        "priceRange": {"low": 0, "high": 0},
        "color": [],
        "size": [TARGET_SIZE_CODE],
        "season": [],
        "material": [],
        "sex": [],
        "categoryFilter": {},
        "identity": [],
        "insiteDescription": "",
        "exist": [
            {
                "title": "尺码",
                "items": [{"sizeValue": "L", "sizeCode": TARGET_SIZE_CODE, "sizeNoSuffix": "005"}],
            }
        ],
        "categoryCode": "utyinhua_m",
        "searchFlag": False,
        "description": "",
    }


def fetch_page(page: int) -> tuple[list[JsonDict], int]:
    """抓取单页优衣库接口结果。

    Args:
        page: 当前页码。

    Returns:
        当前页原始商品列表和接口返回的商品总数。
    """

    payload = json.dumps(build_request_payload(page)).encode("utf-8")
    request = Request(API_URL, data=payload, headers=REQUEST_HEADERS, method="POST")
    with urlopen(request, timeout=30) as response:
        parsed = cast(JsonDict, json.load(response))
    return parse_page_response(parsed)


def parse_page_response(payload: Mapping[str, Any]) -> tuple[list[JsonDict], int]:
    """解析接口响应并提取商品列表。

    Args:
        payload: 反序列化后的接口响应。

    Returns:
        当前页商品列表和总商品数。
    """

    response_items = payload.get("resp")
    if not isinstance(response_items, list) or len(response_items) < 3:
        raise ValueError("优衣库接口响应缺少 resp 数组。")
    page_items = response_items[1]
    summary = response_items[2]
    if not isinstance(page_items, list) or not isinstance(summary, Mapping):
        raise ValueError("优衣库接口响应结构异常。")
    product_sum = summary.get("productSum")
    if not isinstance(product_sum, int):
        raise ValueError("优衣库接口响应缺少 productSum。")
    return [ensure_json_dict(item) for item in page_items], product_sum


def fetch_products(fetcher: PageFetcher) -> list[Product]:
    """抓取并标准化全部分页商品。

    Args:
        fetcher: 单页抓取函数。

    Returns:
        去重并排序后的标准化商品列表。
    """

    raw_items: list[JsonDict] = []
    page = 1
    total_products = PAGE_SIZE
    while True:
        page_items, total_products = fetcher(page)
        raw_items.extend(page_items)
        if not page_items or page * PAGE_SIZE >= total_products:
            break
        page += 1
    products = [normalize_product(item) for item in raw_items]
    return deduplicate_products(products)


def normalize_product(raw_product: Mapping[str, Any]) -> Product:
    """标准化单个商品字段。

    Args:
        raw_product: 接口原始商品对象。

    Returns:
        标准化后的商品对象。
    """

    product_code = require_str(raw_product, "productCode")
    image_path = require_str(raw_product, "mainPic")
    return Product(
        code=require_str(raw_product, "code"),
        product_code=product_code,
        name=require_str(raw_product, "name"),
        min_price=require_float(raw_product, "minPrice"),
        origin_price=require_float(raw_product, "originPrice"),
        image_url=build_image_url(image_path),
        detail_url=DETAIL_URL_TEMPLATE.format(product_code=product_code),
        style_texts=read_string_list(raw_product, "styleText4zhCN"),
        size_codes=read_string_list(raw_product, "size"),
    )


def deduplicate_products(products: Iterable[Product]) -> list[Product]:
    """按商品编码去重并保持价格升序。"""

    deduplicated: dict[str, Product] = {}
    for product in products:
        deduplicated.setdefault(product.code, product)
    return sorted(deduplicated.values(), key=lambda item: (item.min_price, item.code))


def filter_target_products(products: Iterable[Product]) -> list[Product]:
    """筛选符合提醒条件的商品。"""

    return [product for product in products if is_target_product(product)]


def is_target_product(product: Product) -> bool:
    """判断商品是否满足提醒条件。"""

    return all(
        (
            "T恤" in product.name,
            product.min_price <= PRICE_THRESHOLD,
            product.origin_price > product.min_price,
            TARGET_SIZE_CODE in product.size_codes,
        )
    )


def load_state(path: Path) -> dict[str, AlertRecord]:
    """读取已提醒状态文件。

    Args:
        path: 状态文件路径。

    Returns:
        以商品编码为键的已提醒记录。
    """

    if not path.exists():
        return {}
    payload = cast(JsonDict, json.loads(path.read_text(encoding="utf-8")))
    raw_alerted = payload.get("alerted_products")
    if not isinstance(raw_alerted, Mapping):
        raise ValueError("状态文件缺少 alerted_products 字段。")
    alerted_products: dict[str, AlertRecord] = {}
    for code, entry in raw_alerted.items():
        if not isinstance(code, str) or not isinstance(entry, Mapping):
            raise ValueError("状态文件中的商品记录格式无效。")
        alerted_products[code] = AlertRecord(
            product_code=require_str(entry, "product_code"),
            name=require_str(entry, "name"),
            alerted_price=require_float(entry, "alerted_price"),
            alerted_at=require_str(entry, "alerted_at"),
        )
    return alerted_products


def save_state(path: Path, alerted_products: Mapping[str, AlertRecord]) -> None:
    """写入已提醒状态文件。

    Args:
        path: 状态文件路径。
        alerted_products: 已提醒商品记录。
    """

    payload = {
        "alerted_products": {
            code: asdict(record) for code, record in sorted(alerted_products.items())
        }
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def find_new_products(
    products: Iterable[Product], alerted_products: Mapping[str, AlertRecord]
) -> list[Product]:
    """筛选本次首次出现的降价商品。"""

    return [product for product in products if product.code not in alerted_products]


def build_updated_state(
    alerted_products: Mapping[str, AlertRecord],
    new_products: Iterable[Product],
    alerted_at: str,
) -> dict[str, AlertRecord]:
    """合并新提醒商品到状态中。"""

    merged = dict(alerted_products)
    for product in new_products:
        merged[product.code] = AlertRecord(
            product_code=product.product_code,
            name=product.name,
            alerted_price=product.min_price,
            alerted_at=alerted_at,
        )
    return merged


def build_email_message(
    products: list[Product], config: MailConfig, image_fetcher: ImageFetcher
) -> EmailMessage:
    """构造包含商品图片的提醒邮件。

    Args:
        products: 需要提醒的商品列表。
        config: 邮件发送配置。
        image_fetcher: 图片抓取函数。

    Returns:
        可直接发送的邮件对象。
    """

    message = EmailMessage()
    message["Subject"] = f"优衣库 UT L码新降价提醒（{len(products)}件）"
    message["From"] = config.mail_from
    message["To"] = config.mail_to
    message.set_content(build_text_body(products))
    html_body, inline_images = build_html_body(products, image_fetcher)
    message.add_alternative(html_body, subtype="html")
    payload = message.get_payload()
    if not isinstance(payload, list) or not payload:
        raise ValueError("邮件结构异常，缺少 HTML 正文。")
    html_part = cast(EmailMessage, payload[-1])
    for image in inline_images:
        maintype, subtype = image.mime_type.split("/", maxsplit=1)
        html_part.add_related(image.data, maintype=maintype, subtype=subtype, cid=image.cid)
    return message


def build_text_body(products: Iterable[Product]) -> str:
    """构造纯文本邮件正文。"""

    lines = ["发现新的优衣库男装 UT 印花系列 L 码降价 T 恤：", ""]
    for product in products:
        lines.extend(
            (
                f"- {product.name}（{product.code}）",
                f"  现价：¥{product.min_price:.0f}，原价：¥{product.origin_price:.0f}",
                f"  链接：{product.detail_url}",
                "",
            )
        )
    return "\n".join(lines).strip()


def build_html_body(
    products: Iterable[Product], image_fetcher: ImageFetcher
) -> tuple[str, list[InlineImage]]:
    """构造 HTML 邮件正文和内联图片。"""

    cards: list[str] = []
    inline_images: list[InlineImage] = []
    for product in products:
        image_src, inline_image = build_image_reference(product.image_url, image_fetcher)
        if inline_image is not None:
            inline_images.append(inline_image)
        cards.append(build_product_card(product, image_src))
    html_body = """
    <html>
      <body style="font-family: Arial, sans-serif; background: #f5f5f5; padding: 24px;">
        <h2 style="margin: 0 0 16px;">发现新的优衣库男装 UT L 码降价 T 恤</h2>
        <div style="display: block;">
          {cards}
        </div>
      </body>
    </html>
    """.strip().format(cards="".join(cards))
    return html_body, inline_images


def build_image_reference(
    image_url: str, image_fetcher: ImageFetcher
) -> tuple[str, InlineImage | None]:
    """生成图片引用，优先使用内联图片。"""

    image_data = image_fetcher(image_url)
    if image_data is None:
        return image_url, None
    cid = make_msgid(domain="uniqlo.local")[1:-1]
    mime_type = guess_mime_type(image_url)
    return f"cid:{cid}", InlineImage(cid=cid, data=image_data, mime_type=mime_type)


def build_product_card(product: Product, image_src: str) -> str:
    """构造单个商品卡片的 HTML。"""

    style_text = " / ".join(product.style_texts) if product.style_texts else "颜色未知"
    return """
    <div style="background: #ffffff; border-radius: 12px; margin: 0 0 16px; overflow: hidden;">
      <img
        src="{image_src}"
        alt="{name}"
        style="display: block; width: 100%; max-width: 480px; height: auto;"
      />
      <div style="padding: 16px 18px;">
        <h3 style="margin: 0 0 12px; font-size: 18px;">{name}</h3>
        <p style="margin: 0 0 8px;">商品编码：{code}</p>
        <p style="margin: 0 0 8px;">颜色：{style_text}</p>
        <p style="margin: 0 0 12px;">
          <strong style="color: #d0021b;">现价：¥{min_price:.0f}</strong>
          <span style="margin-left: 8px; color: #666666; text-decoration: line-through;">
            原价：¥{origin_price:.0f}
          </span>
        </p>
        <a
          href="{detail_url}"
          style="display: inline-block; color: #ffffff; background: #111111; padding: 10px 14px;
                 text-decoration: none; border-radius: 8px;"
        >
          查看商品详情
        </a>
      </div>
    </div>
    """.strip().format(
        image_src=escape(image_src, quote=True),
        name=escape(product.name),
        code=escape(product.code),
        style_text=escape(style_text),
        min_price=product.min_price,
        origin_price=product.origin_price,
        detail_url=escape(product.detail_url, quote=True),
    )


def send_email(message: EmailMessage, config: MailConfig) -> None:
    """通过 QQ SMTP 发送提醒邮件。"""

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=context) as smtp:
        smtp.login(config.mail_from, config.smtp_auth_code)
        smtp.send_message(message)


def download_image(image_url: str) -> bytes | None:
    """下载商品图片，失败时返回空值。"""

    request = Request(image_url, headers={"User-Agent": REQUEST_HEADERS["User-Agent"]})
    try:
        with urlopen(request, timeout=20) as response:
            return cast(bytes, response.read())
    except (HTTPError, URLError, TimeoutError, ValueError):
        return None


def process_watch(
    *,
    config: MailConfig,
    state_path: Path,
    fetcher: PageFetcher,
    image_fetcher: ImageFetcher,
    sender: MessageSender,
    now: datetime | None = None,
) -> int:
    """执行一次抓取、发送和状态更新流程。

    Args:
        config: 邮件发送配置。
        state_path: 状态文件路径。
        fetcher: 单页抓取函数。
        image_fetcher: 图片抓取函数。
        sender: 邮件发送函数。
        now: 注入的当前时间，便于测试。

    Returns:
        本次新提醒商品数量。
    """

    all_products = fetch_products(fetcher)
    target_products = filter_target_products(all_products)
    alerted_products = load_state(state_path)
    new_products = find_new_products(target_products, alerted_products)
    if not new_products:
        print("没有新的符合条件商品。")
        return 0
    message = build_email_message(new_products, config, image_fetcher)
    sender(message, config)
    alerted_at = (now or datetime.now(UTC)).isoformat()
    updated_state = build_updated_state(alerted_products, new_products, alerted_at)
    save_state(state_path, updated_state)
    print(f"已发送提醒邮件，商品数量：{len(new_products)}")
    return len(new_products)


def load_mail_config_from_env() -> MailConfig:
    """从环境变量加载邮件发送配置。"""

    return MailConfig(
        mail_from=read_required_env(MAIL_FROM_ENV),
        mail_to=read_required_env(MAIL_TO_ENV),
        smtp_auth_code=read_required_env(MAIL_AUTH_CODE_ENV),
    )


def read_required_env(name: str) -> str:
    """读取必填环境变量。"""

    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"缺少必填环境变量 {name}。")
    return value


def build_image_url(image_path: str) -> str:
    """将图片路径拼接为完整 URL。"""

    if image_path.startswith("http://") or image_path.startswith("https://"):
        return image_path
    return f"{BASE_URL}{image_path}"


def require_str(payload: Mapping[str, Any], key: str) -> str:
    """读取必填字符串字段。"""

    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"接口字段 {key} 缺失或为空。")
    return value.strip()


def require_float(payload: Mapping[str, Any], key: str) -> float:
    """读取必填数值字段。"""

    value = payload.get(key)
    if not isinstance(value, int | float):
        raise ValueError(f"接口字段 {key} 缺失或不是数值。")
    return float(value)


def read_string_list(payload: Mapping[str, Any], key: str) -> tuple[str, ...]:
    """读取字符串数组字段。"""

    value = payload.get(key)
    if not isinstance(value, list):
        return ()
    items = [str(item).strip() for item in value if str(item).strip()]
    return tuple(items)


def ensure_json_dict(value: Any) -> JsonDict:
    """确保接口项目是 JSON 对象。"""

    if not isinstance(value, Mapping):
        raise ValueError("接口商品项不是对象。")
    return dict(value)


def guess_mime_type(image_url: str) -> str:
    """根据图片 URL 推断 MIME 类型。"""

    guessed_type, _ = mimetypes.guess_type(urlparse(image_url).path)
    return guessed_type or "image/jpeg"


def main() -> int:
    """命令行入口。"""

    config = load_mail_config_from_env()
    process_watch(
        config=config,
        state_path=STATE_PATH,
        fetcher=fetch_page,
        image_fetcher=download_image,
        sender=send_email,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
