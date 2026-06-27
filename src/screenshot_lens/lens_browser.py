"""
Google Lens search via Playwright Chromium.

Architecture:
  - Uses a PERSISTENT Chromium profile so Google login cookies survive
    across app restarts.
  - First time: call open_browser_for_login() (or disable Headless) so the
    user can log into their Google account. Subsequent runs use saved cookies.
  - Upload: Playwright warms up the session, then requests POSTs the image
    with those cookies, then Playwright renders the result page.
"""
from __future__ import annotations
import asyncio
from concurrent.futures import Future
import io
import os
from pathlib import Path
import shutil
import tempfile
import threading
from urllib.parse import quote_plus

from PIL import Image
import requests


_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/148.0.0.0 Safari/537.36"
)
_ACCEPT_LANGUAGE = "vi-VN,vi;q=0.9,en;q=0.8"
_UPLOAD_URL = "https://lens.google.com/upload?hl=vi"
_GEMINI_DETAIL_PROMPT = (
    "Hãy đọc kỹ toàn bộ chữ và nội dung trong ảnh, rồi trả lời bằng tiếng Việt. "
    "Ưu tiên nội dung chữ, câu hỏi, đáp án và dữ kiện trong ảnh; đừng chỉ mô tả bố cục hoặc hình thức của ảnh. "
    "Nếu ảnh chứa câu hỏi hoặc bài trắc nghiệm: chọn đáp án đúng trước, giải thích vì sao đúng, "
    "phân tích từng lựa chọn sai và bổ sung kiến thức liên quan cần biết. "
    "Nếu ảnh không phải câu hỏi: tóm tắt nội dung chữ trong ảnh và giải thích ý chính, khái niệm, bối cảnh hoặc hàm ý quan trọng."
)

CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    str(Path.home() / r"AppData\Local\Google\Chrome\Application\chrome.exe"),
]

# Persistent profile: project-local Chrome user data, same pattern as GeminiEdu.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_PROFILE_DIR = _PROJECT_ROOT / "chrome_profile" / "ScreenshotLensSession"

# ── Headless toggle ──────────────────────────────────────────────────────────
_HEADLESS: bool = True


class _GoogleBlockedError(RuntimeError):
    """Raised when Google shows a CAPTCHA/sorry page or returns 403."""


def find_chrome() -> str | None:
    for path in CHROME_PATHS:
        if Path(path).exists():
            return path
    return None


def set_headless(value: bool) -> None:
    """Toggle headless mode. Effective from the next search call."""
    global _HEADLESS, _SESSION
    if _HEADLESS == value:
        return
    _HEADLESS = value
    try:
        _SESSION.close()
    except Exception:
        pass
    _SESSION = _LensBrowserSession()


# ── Result data structure ────────────────────────────────────────────────────

class LensResult:
    def __init__(self):
        self.ocr_text: str = ""
        self.visual_matches: list[dict] = []
        self.related_searches: list[str] = []
        self.lens_url: str = ""
        self.error: str = ""

    def to_display_text(self) -> str:
        parts: list[str] = []
        if self.ocr_text:
            parts.append(f"✨ THÔNG TIN GOOGLE GEMINI\n{'─'*40}\n{self.ocr_text}")
        if self.error:
            parts.append(f"❌ LỖI: {self.error}")
        if not parts:
            parts.append("(Không lấy được thông tin Gemini)")
        return "\n\n".join(parts)


# ── Playwright browser session ───────────────────────────────────────────────

class _LensBrowserSession:
    """Long-lived Playwright session with PERSISTENT profile.

    Google login cookies are stored in the project-local Chrome profile
    (chrome_profile/ScreenshotLensSession)
    and survive app restarts.  The user only needs to log in once via the
    'Đăng nhập Google' button (which opens Chrome in visible mode).
    """

    def __init__(self):
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ready = threading.Event()
        self._start_lock = threading.Lock()
        self._playwright = None
        self._context = None
        self._page = None
        self._context_headless = None
        self._init_lock = None
        self._profile_dir = _PROFILE_DIR
        self._profile_dir.mkdir(parents=True, exist_ok=True)

    # ── Public sync API ──────────────────────────────────────────────────────

    def warm_up(self) -> None:
        """Pre-launch the browser so the first search is faster."""
        future = self._submit(self._ensure_page())
        future.add_done_callback(lambda f: f.exception())

    def upload_and_extract(self, img_bytes: bytes, prompt: str | None, timeout_ms: int) -> dict:
        """Upload image via Playwright and extract Gemini result. Blocking."""
        future = self._submit(self._upload_and_extract(img_bytes, prompt, timeout_ms))
        return future.result(timeout=max(150, int(timeout_ms / 1000) + 130))

    def close(self) -> None:
        if not self._loop:
            return
        try:
            self._submit(self._close_async()).result(timeout=10)
        except Exception:
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=5)
        # NOTE: do NOT delete _profile_dir — it holds Google login cookies!

    def open_for_login(self) -> None:
        """Open a visible Chrome window so the user can log into Google.
        Call this once; subsequent runs use the saved cookies (headless=True).
        """
        future = self._submit(self._open_login_page())
        future.result(timeout=300)  # wait up to 5 min for user to finish login

    def _remove_profile_locks(self) -> None:
        for lock_name in ("LOCK", "SingletonLock"):
            for item in self._profile_dir.rglob(lock_name):
                try:
                    item.unlink()
                except Exception:
                    pass

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _submit(self, coro) -> Future:
        self._ensure_loop()
        assert self._loop is not None
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def _ensure_loop(self) -> None:
        if self._loop and self._thread and self._thread.is_alive():
            return
        with self._start_lock:
            if self._loop and self._thread and self._thread.is_alive():
                return
            self._ready.clear()
            self._thread = threading.Thread(
                target=self._run_loop,
                name="ScreenshotLensPlaywright",
                daemon=True,
            )
            self._thread.start()
            self._ready.wait(timeout=10)
            if not self._loop:
                raise RuntimeError("Không khởi động được Playwright event loop")

    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._ready.set()
        loop.run_forever()
        loop.close()

    async def _ensure_page(self, headless: bool | None = None):
        if self._init_lock is None:
            self._init_lock = asyncio.Lock()

        desired_headless = _HEADLESS if headless is None else headless
        async with self._init_lock:
            if (
                self._context
                and self._context_headless != desired_headless
            ):
                await self._context.close()
                self._context = None
                self._page = None
                self._context_headless = None

            if self._page and not self._page.is_closed():
                return self._page

            from playwright.async_api import async_playwright

            self._profile_dir.mkdir(parents=True, exist_ok=True)
            self._remove_profile_locks()

            if self._playwright is None:
                self._playwright = await async_playwright().start()

            if self._context is None:
                chrome_exe = find_chrome()
                launch_kwargs = {
                    "user_data_dir": str(self._profile_dir),
                    "headless": desired_headless,
                    "viewport": {"width": 1280, "height": 900},
                    "user_agent": _USER_AGENT,
                    "locale": "vi-VN",
                    "timezone_id": "Asia/Ho_Chi_Minh",
                    "accept_downloads": False,
                    "extra_http_headers": {"Accept-Language": _ACCEPT_LANGUAGE},
                    "ignore_https_errors": True,
                    "args": [
                        "--disable-blink-features=AutomationControlled",
                        "--disable-infobars",
                        "--no-first-run",
                        "--no-default-browser-check",
                        "--exclude-switches=enable-automation",
                        "--password-store=basic",
                        "--lang=vi-VN,vi",
                    ],
                    "ignore_default_args": [
                        "--disable-blink-features=AutomationControlled",
                        "--disable-dev-shm-usage",
                        "--no-sandbox",
                        "--enable-automation",
                    ],
                }
                if chrome_exe:
                    launch_kwargs["executable_path"] = chrome_exe
                if not desired_headless:
                    launch_kwargs["slow_mo"] = 30

                self._context = await self._playwright.chromium.launch_persistent_context(
                    **launch_kwargs
                )
                await self._context.add_init_script("""
                    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                    delete navigator.__proto__.webdriver;
                    Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] });
                    Object.defineProperty(navigator, 'languages', { get: () => ['vi-VN','vi','en-US','en'] });
                    window.chrome = {
                        app: { isInstalled: false },
                        runtime: { onConnect: { addListener: () => {} }, onMessage: { addListener: () => {} } },
                        loadTimes: function() {}, csi: function() {},
                    };
                """)
                self._context_headless = desired_headless

            self._page = (
                self._context.pages[0]
                if self._context.pages
                else await self._context.new_page()
            )
            return self._page

    async def _open_login_page(self) -> None:
        """Open Google login in a VISIBLE window, wait for user to finish."""
        # Temporarily close headless context and reopen visible
        if self._context:
            await self._context.close()
            self._context = None
            self._page = None
            self._context_headless = None

        from playwright.async_api import async_playwright
        if self._playwright is None:
            self._playwright = await async_playwright().start()

        self._profile_dir.mkdir(parents=True, exist_ok=True)
        self._remove_profile_locks()
        chrome_exe = find_chrome()
        launch_kwargs = {
            "user_data_dir": str(self._profile_dir),
            "headless": False,
            "viewport": {"width": 1280, "height": 900},
            "user_agent": _USER_AGENT,
            "locale": "vi-VN",
            "timezone_id": "Asia/Ho_Chi_Minh",
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--no-first-run",
                "--no-default-browser-check",
                "--exclude-switches=enable-automation",
                "--lang=vi-VN,vi",
            ],
            "ignore_default_args": [
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
                "--enable-automation",
            ],
            "ignore_https_errors": True,
            "slow_mo": 30,
        }
        if chrome_exe:
            launch_kwargs["executable_path"] = chrome_exe

        # Always open visible for login
        ctx = await self._playwright.chromium.launch_persistent_context(**launch_kwargs)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto("https://accounts.google.com/", wait_until="domcontentloaded")

        # Wait until the user is logged in (URL changes away from accounts.google.com)
        # or up to 5 minutes
        try:
            await page.wait_for_url(
                lambda url: "myaccount.google.com" in url or "google.com/search" in url,
                timeout=300_000,
            )
        except Exception:
            pass

        await ctx.close()
        self._context = None
        self._page = None

    async def _upload_and_extract(
        self,
        img_bytes: bytes,
        prompt: str | None,
        timeout_ms: int,
    ) -> dict:
        """Upload to Lens, then render the result page with Playwright."""
        page = await self._ensure_page()
        page.set_default_timeout(timeout_ms)
        prompt = prompt or _GEMINI_DETAIL_PROMPT

        # Lấy cookies từ Playwright profile (có Google login) inject vào requests
        pw_cookies = await self._context.cookies("https://lens.google.com")

        session = requests.Session()
        session.headers.update(
            {
                "User-Agent": _USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": _ACCEPT_LANGUAGE,
            }
        )
        for c in pw_cookies:
            session.cookies.set(c["name"], c["value"], domain=c.get("domain", ".google.com"))

        upload_s = max(10, min(20, timeout_ms // 1000))
        resp = session.post(
            _UPLOAD_URL,
            data={"hl": "vi", "q": prompt},
            files={"encoded_image": ("screenshot.jpg", img_bytes, "image/jpeg")},
            allow_redirects=False,
            timeout=upload_s,
        )

        final_url = resp.headers.get("Location", "")
        if resp.status_code not in {301, 302, 303, 307, 308} or not final_url:
            raise RuntimeError(f"Upload thất bại: Google Lens trả HTTP {resp.status_code}")
        if final_url.startswith("/"):
            final_url = "https://lens.google.com" + final_url
        if "google.com" not in final_url:
            raise RuntimeError(f"Upload thất bại, URL không hợp lệ: {final_url!r}")

        # Sync cookies từ requests response ngược lại vào Playwright context.
        # Chỉ set các field bắt buộc để tránh lỗi Protocol "Invalid cookie fields".
        new_cookies = []
        for c in session.cookies:
            domain = c.domain or ".google.com"
            if domain and not domain.startswith(".") and not domain.startswith("lens") and not domain.startswith("accounts"):
                domain = "." + domain
            cookie: dict = {
                "name": c.name,
                "value": c.value,
                "domain": domain,
                "path": c.path or "/",
            }
            new_cookies.append(cookie)
        if new_cookies:
            try:
                await self._context.add_cookies(new_cookies)
            except Exception:
                pass  # cookies đã có trong profile, bỏ qua nếu lỗi

        nav_resp = await page.goto(
            final_url,
            wait_until="domcontentloaded",
            timeout=timeout_ms,
        )

        if nav_resp and nav_resp.status == 403:
            if _HEADLESS:
                return await self._upload_with_visible_browser(img_bytes, prompt, timeout_ms)
            raise _GoogleBlockedError("Google trả về 403.")

        await page.wait_for_timeout(800)

        body_text = await page.locator("body").inner_text(timeout=min(timeout_ms, 8000))
        if "/sorry/" in page.url or "unusual traffic" in body_text.lower() or "lưu lượng truy cập bất thường" in body_text.lower():
            if _HEADLESS:
                return await self._upload_with_visible_browser(img_bytes, prompt, timeout_ms)
            raise _GoogleBlockedError("Google CAPTCHA. Hãy giải CAPTCHA trong cửa sổ Chromium.")

        # ── 5. Extract Gemini result ─────────────────────────────────────────
        extracted = await self._wait_for_gemini_result(page, timeout_ms)
        extracted["url"] = page.url
        return extracted

    async def _upload_with_visible_browser(
        self,
        img_bytes: bytes,
        prompt: str,
        timeout_ms: int,
    ) -> dict:
        """Fallback khi headless bị 403/CAPTCHA.
        Đóng context headless hiện tại → mở visible → lấy kết quả →
        reset self._context về None để lần search tiếp tạo lại headless.
        """
        # Đóng context headless đang giữ profile lock
        if self._context:
            try:
                await self._context.close()
            except Exception:
                pass
            self._context = None
            self._page = None
            self._context_headless = None

        self._remove_profile_locks()

        # Mở visible context trên cùng profile (có cookies Google)
        page = await self._ensure_page(headless=False)
        page.set_default_timeout(timeout_ms)

        temp_path = Path(tempfile.gettempdir()) / f"screenshot-lens-upload-{os.getpid()}.jpg"
        temp_path.write_bytes(img_bytes)
        try:
            await page.goto("https://lens.google.com/?hl=vi", wait_until="domcontentloaded", timeout=timeout_ms)
            await page.wait_for_timeout(1000)

            file_input = page.locator(
                'input[name="encoded_image"], input[type="file"][accept*="image"]'
            ).last
            await file_input.set_input_files(str(temp_path), timeout=timeout_ms)
            await page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
            await page.wait_for_timeout(1500)

            if "/sorry/" in page.url:
                await page.bring_to_front()
                captcha_wait_ms = max(20_000, min(120_000, timeout_ms * 3))
                await page.wait_for_url(lambda url: "/sorry/" not in url, timeout=captcha_wait_ms)
                await page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)

            body_text = await page.locator("body").inner_text(timeout=min(timeout_ms, 8000))
            if "/sorry/" in page.url or "unusual traffic" in body_text.lower() or "lưu lượng truy cập bất thường" in body_text.lower():
                raise _GoogleBlockedError(
                    "Google đang yêu cầu xác minh CAPTCHA. Cửa sổ Chromium đã được mở; hãy giải CAPTCHA rồi thử tìm kiếm lại."
                )

            if "q=" not in page.url and prompt:
                separator = "&" if "?" in page.url else "?"
                await page.goto(
                    f"{page.url}{separator}q={quote_plus(prompt)}",
                    wait_until="domcontentloaded",
                    timeout=timeout_ms,
                )

            extracted = await self._wait_for_gemini_result(page, timeout_ms)
            extracted["url"] = page.url
            return extracted
        finally:
            # Đóng visible context → lần search tiếp _ensure_page sẽ tạo lại headless
            if self._context:
                try:
                    await self._context.close()
                except Exception:
                    pass
                self._context = None
                self._page = None
                self._context_headless = None
            try:
                temp_path.unlink(missing_ok=True)
            except Exception:
                pass


    async def _wait_for_gemini_result(self, page, timeout_ms: int) -> dict:
        started_at = asyncio.get_running_loop().time()
        deadline = started_at + min(timeout_ms / 1000, 25)
        best = {"ocrText": "", "visualMatches": [], "relatedSearches": [], "isGemini": False}
        last_text = ""
        stable_count = 0

        while asyncio.get_running_loop().time() < deadline:
            extracted = await page.evaluate(_EXTRACT_JS)
            if not isinstance(extracted, dict):
                await page.wait_for_timeout(400)
                continue

            text = (extracted.get("ocrText") or "").strip()
            is_gemini = bool(extracted.get("isGemini"))
            looks_useful = self._looks_like_gemini_result(text)

            # Keep best: prefer isGemini=True, then longer text
            prev_text = (best.get("ocrText") or "").strip()
            if text and (
                (is_gemini and not best.get("isGemini"))
                or (is_gemini == best.get("isGemini") and len(text) > len(prev_text))
            ):
                best = extracted

            if text and text == last_text:
                stable_count += 1
            else:
                stable_count = 0
                last_text = text

            elapsed = asyncio.get_running_loop().time() - started_at

            # Early exit: complete stable isGemini result
            if is_gemini and looks_useful and stable_count >= 1 and elapsed >= 3 and self._looks_complete_gemini_result(text):
                return extracted

            # Stable isGemini (not still streaming)
            if is_gemini and looks_useful and stable_count >= 4 and not text.rstrip().endswith((":", "(", ",")):
                return extracted

            # Fallback: any stable content after 8 s
            if looks_useful and stable_count >= 6 and elapsed >= 8:
                return extracted

            await page.wait_for_timeout(500)

        # Last resort: chỉ dùng những gì đã extract được, KHÔNG dump raw body
        # (raw body chứa cả web results, nav links... gây nhiễu)
        if not (best.get("ocrText") or "").strip():
            best["ocrText"] = "⏳ Gemini chưa trả lời. Hãy thử tìm kiếm lại."

        return best

    @staticmethod
    def _looks_like_gemini_result(text: str) -> bool:
        value = (text or "").strip()
        if not value or len(value) < 20:
            return False
        if value in {
            "Thông tin về hình ảnh này", "Kết quả tìm kiếm",
            "Hình ảnh trùng khớp", "AI overview", "Thông tin tổng quan",
        }:
            return False
        return True

    @staticmethod
    def _looks_complete_gemini_result(text: str) -> bool:
        value = (text or "").strip()
        if len(value) < 200 or value.rstrip().endswith(":"):
            return False
        return True

    async def _close_async(self) -> None:
        if self._context:
            await self._context.close()
            self._context = None
            self._page = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None


_SESSION = _LensBrowserSession()


def warm_up_lens_browser() -> None:
    _SESSION.warm_up()


def close_lens_browser() -> None:
    _SESSION.close()


import atexit
atexit.register(close_lens_browser)


# ── JavaScript injected into page to extract structured data ─────────────────

_EXTRACT_JS = r"""
() => {
    const out = { ocrText: '', visualMatches: [], relatedSearches: [], isGemini: false };

    const clean = (text) => (text || '').replace(/\s+/g, ' ').trim();

    // ── Patterns dòng luôn bỏ qua ──────────────────────────────────────────
    const skipPatterns = [
        /^Bỏ qua/i, /^Hỗ trợ/i, /^Phản hồi$/i, /^Đăng nhập/i,
        /^Chế độ AI$/i, /^Tất cả$/i, /^Mọi \w/i,
        /^(Giờ|24 giờ|Tuần|Tháng|Năm) qua$/i,
        /^Việt Nam$/i, /^Trợ giúp/i, /^Quyền riêng tư/i, /^Điều khoản/i,
        /^Thông tin tổng quan$/i, /^AI overview$/i,
        /^Tìm kiếm$/i, /^Tìm$/i, /^Tìm kiếm bằng Google$/i, /^Lens$/i,
        /^Chia sẻ$/i, /^Lưu$/i, /^Thêm$/i, /^Xem thêm$/i,
    ];

    const isSkipped = (text) => {
        const v = clean(text);
        return !v || v.length < 4
            || skipPatterns.some(p => p.test(v))
            || /Cảm ơn bạn|Ý kiến phản hồi|Chính sách quyền riêng tư|Đang suy nghĩ|thinking a little longer|still thinking|AI có thể mắc sai sót|xác minh câu trả lời/i.test(v);
    };

    // ── Patterns dừng collect ───────────────────────────────────────────────
    const stopPatterns = [
        /^Kết quả tìm kiếm/i, /^Search results/i,
        /^Hình ảnh trùng khớp/i, /^Visual matches/i,
        /^Related searches/i, /^Kết quả khớp/i,
        /^Thông tin về hình ảnh/i, /^About this image/i,
        /^Hiển thị tất cả/i, /^Hiển thị thêm/i,
        /^Dịch$/i, /^Dịch thuật$/i,
        /^Đường liên kết để chọn trang/i, /^Mọi ngôn ngữ/i,
        /^Tìm những trang/i, /^Nguyên văn/i,
        /^Tìm kiếm liên quan/i, /^Có thể bạn muốn tìm/i,
        /^Nguồn web/i, /^Web results/i,
    ];

    // ── Nhận diện dòng là nguồn web / kết quả tìm kiếm ────────────────────
    // Dạng URL
    const isUrl = (t) => /^(https?:\/\/|www\.)/i.test(t);
    // Dạng domain thuần: google.com, vn, edu...
    const isDomain = (t) => /^[a-z0-9-]+\.[a-z]{2,}(\/|$)/i.test(t);
    // Dấu · thường xuất hiện trong "Nguồn · N giờ trước" hoặc "Facebook · ..."
    const hasBulletSource = (t) => /·/.test(t);
    // Dòng chỉ là tên nguồn đơn lẻ phổ biến
    const isSourceName = (t) => /^(Facebook|YouTube|TikTok|Wikipedia|Studocu|Prep Education|VTV7|Google Play|App Store|Coursera|Khan Academy|BBC|CNN|VNExpress|Tuổi Trẻ|Thanh Niên|Dân Trí|Zing|Kenh14|24h|Báo|Tạp chí|Medium|Reddit|Twitter|X\.com|Instagram|Zalo|Quora|Stack Overflow|GitHub|Gitlab|npm|PyPI|Docs|Documentation)(\s|$)/i.test(t);
    // Timestamp dạng "2 giờ trước", "3 ngày trước", "Jan 2024"
    const isTimestamp = (t) => /^\d+\s*(giờ|ngày|tuần|tháng|phút|giây|năm)\s*(trước|qua)$/i.test(t) || /^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{4}$/i.test(t);
    // Dạng "N kết quả" hoặc "Khoảng N..."
    const isResultCount = (t) => /^(Khoảng\s+)?\d[\d\.,]+\s*(kết quả|results)/i.test(t);
    // Dòng chỉ số thứ tự video / timestamp ">1" hay "1:23"
    const isVideoMeta = (t) => /^>\d+$/.test(t) || /^\d{1,2}:\d{2}(:\d{2})?$/.test(t);

    const isWebLine = (t) => {
        const v = clean(t);
        return isUrl(v) || isDomain(v) || hasBulletSource(v)
            || isSourceName(v) || isTimestamp(v) || isResultCount(v) || isVideoMeta(v);
    };

    // ── Nhận diện cụm "tiêu đề bài web + nguồn" liền nhau ─────────────────
    // Nếu dòng hiện tại KHÔNG phải web nhưng dòng tiếp theo là nguồn web → cả 2 là web result
    const isWebResultBlock = (line, next1, next2) => {
        if (isWebLine(next1)) return true;
        if (isWebLine(next2) && (isSourceName(clean(next1)) || isTimestamp(clean(next1)))) return true;
        return false;
    };

    const rawLines = (document.body.innerText || '').split('\n').map(clean).filter(Boolean);

    // ── Strategy 1: tìm heading AI overview ───────────────────────────────
    let startIdx = rawLines.findIndex(
        (l) => /Thông tin tổng quan|AI overview|AI-generated overview/i.test(l)
    );
    const hasAiHeading = startIdx >= 0;
    if (hasAiHeading) startIdx += 1;

    // ── Strategy 2: tìm dòng đầu tiên có dấu hiệu câu trả lời AI ─────────
    if (startIdx < 0) {
        startIdx = rawLines.findIndex(
            (l) => /^(Dựa trên|Đáp án(?: đúng)?[\s:]|Câu hỏi:|Nội dung trong ảnh|Lời giải[\s:]|Giải thích[\s:]|Phân tích[\s:]|Tóm tắt[\s:]|Trả lời[\s:]|Theo |Hình ảnh |Đây là |Trong ảnh)/i.test(l)
        );
    }

    const collected = [];

    if (startIdx >= 0) {
        for (let i = startIdx; i < rawLines.length; i++) {
            const line = rawLines[i];
            const next1 = rawLines[i + 1] || '';
            const next2 = rawLines[i + 2] || '';

            // Dừng ngay khi gặp section heading
            if (stopPatterns.some(p => p.test(line))) break;

            // Dừng khi bản thân dòng này là web line
            if (isWebLine(line)) break;

            // Dừng khi dòng này là tiêu đề của một web result block
            if (collected.length > 0 && isWebResultBlock(line, next1, next2)) break;

            // Bỏ qua dòng rác UI
            if (isSkipped(line)) continue;
            if (line.length < 5) continue;

            collected.push(line);
            if (collected.length >= 80) break;
        }
    }

    const joined = collected.join('\n').trim();

    if (joined) {
        out.ocrText = joined;
        const hasAnswerSignal = /(Đáp án|Giải thích|Câu hỏi|Lựa chọn|Trả lời|Lời giải|Phân tích|Vì sao|Tóm tắt|Dựa trên|Trong ảnh|Đây là|Nội dung)/i.test(joined);
        out.isGemini = hasAnswerSignal;
    }

    return out;
}
"""


# ── Public sync API ──────────────────────────────────────────────────────────

def search_with_playwright(
    img: Image.Image,
    prompt: str | None = None,   # kept for API compat, not used
    timeout_ms: int = 30000,
) -> LensResult:
    """Synchronous wrapper. Call from any thread.

    Converts PIL Image → JPEG bytes, then lets Playwright upload it to
    lens.google.com via the real browser UI (no requests library).
    """
    buf = io.BytesIO()
    rgb = img.convert("RGB")
    if max(rgb.size) > 1600:
        rgb.thumbnail((1600, 1600), Image.LANCZOS)
    rgb.save(buf, format="JPEG", quality=85, optimize=True)
    img_bytes = buf.getvalue()

    result = LensResult()

    try:
        extracted = _SESSION.upload_and_extract(img_bytes, prompt, timeout_ms)
    except Exception as exc:
        result.error = str(exc)
        return result

    result.lens_url = extracted.get("url") or ""
    result.ocr_text = extracted.get("ocrText", "")
    result.visual_matches = extracted.get("visualMatches", [])
    result.related_searches = extracted.get("relatedSearches", [])
    return result
