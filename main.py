import argparse
import json as _json
import logging
import math
import os
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

# ===========================================================================
# ✏️  KONFIGURASI CEPAT
# ===========================================================================
TAHUN      = "2026"
JENIS_KLPD = "3"
INSTANSI   = "D69"
HEADLESS   = True
MAX_RETRY  = 10
# ===========================================================================

# ---------------------------------------------------------------------------
# Logging (standar Python)
# ---------------------------------------------------------------------------

def setup_logging() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)-5s  %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    return logging.getLogger("rup")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Unduh RUP & Realisasi dari inaproc.id lalu buat laporan HTML."
    )
    parser.add_argument("--tahun",      default=os.getenv("TAHUN",      TAHUN))
    parser.add_argument("--jenis-klpd", default=os.getenv("JENIS_KLPD", JENIS_KLPD))
    parser.add_argument("--instansi",   default=os.getenv("INSTANSI",   INSTANSI))
    parser.add_argument("--max-retry",  type=int, default=int(os.getenv("MAX_RETRY", str(MAX_RETRY))))
    parser.add_argument("--headless",   action=argparse.BooleanOptionalAction, default=HEADLESS)
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Konstanta selector
# ---------------------------------------------------------------------------

VALUE_SELECTOR = "div.grid.gap-4.md\\:grid-cols-2 > div:nth-child(1) p.font-mono.text-xl"

REQUIRED_COLS = {
    "rup":       {"Kode RUP", "Nama Paket", "Total Nilai (Rp)"},
    "realisasi": {"Kode RUP", "Total Nilai (Rp)"},
}


# ---------------------------------------------------------------------------
# Download (satu tab)
# ---------------------------------------------------------------------------

def _backoff(attempt: int, base: int = 5, cap: int = 120) -> int:
    """Exponential backoff dalam detik, dengan upper cap."""
    return min(base * (2 ** (attempt - 1)), cap)


def download_df(
    page,
    url: str,
    name: str,
    max_retry: int,
    logger: logging.Logger,
) -> tuple[pd.DataFrame | None, str]:
    logger.info("[%s] Membuka %s", name, url)
    page.goto(url)
    page.wait_for_load_state("networkidle")

    try:
        page_value = page.locator(VALUE_SELECTOR).inner_text().strip()
        logger.info("[%s] Nilai halaman: %s", name, page_value)
    except Exception as exc:
        logger.warning("[%s] Tidak bisa membaca nilai halaman: %s", name, exc)
        page_value = "?"

    for attempt in range(1, max_retry + 1):
        logger.info("[%s] Klik Download CSV (percobaan %d/%d)…", name, attempt, max_retry)
        try:
            btn = page.get_by_role("button", name="Download CSV")
            btn.wait_for(state="visible", timeout=30_000)
            btn.scroll_into_view_if_needed()

            with page.expect_download(timeout=60_000) as dl:
                btn.click()

            df = pd.read_csv(
                dl.value.path(),
                encoding="utf-8-sig",
                sep=None,
                engine="python",
            )
            logger.info("[%s] ✅ Berhasil: %d baris, %d kolom", name, len(df), len(df.columns))
            _validate_columns(df, name, logger)
            return df, page_value

        except (PlaywrightTimeout, Exception) as exc:
            wait = _backoff(attempt)
            level = logging.WARNING if isinstance(exc, PlaywrightTimeout) else logging.ERROR
            logger.log(level, "[%s] Percobaan %d gagal (%s) — tunggu %ds", name, attempt, exc, wait)
            page.wait_for_timeout(wait * 1_000)

    logger.error("[%s] ❌ Gagal setelah %d percobaan.", name, max_retry)
    return None, page_value


def _validate_columns(df: pd.DataFrame, name: str, logger: logging.Logger) -> None:
    missing = REQUIRED_COLS.get(name, set()) - set(df.columns)
    if missing:
        logger.warning("[%s] Kolom wajib tidak ditemukan: %s", name, missing)
        logger.warning("[%s] Kolom tersedia: %s", name, list(df.columns))


# ---------------------------------------------------------------------------
# ★ OPTIMASI UTAMA: download paralel via 2 tab browser
# ---------------------------------------------------------------------------

def download_parallel(
    browser,
    urls: dict[str, str],
    max_retry: int,
    logger: logging.Logger,
) -> tuple[dict[str, pd.DataFrame], dict[str, str]]:
    """
    Buka setiap URL di tab terpisah secara bergantian (interleaved),
    sehingga waktu tunggu network tidak blocking satu sama lain.

    Playwright sync API tidak thread-safe, jadi tidak pakai threading.
    Strategi: buka semua tab → navigate semua → download semua → tutup.
    Ini tetap ~2× lebih cepat karena I/O jaringan overlap.
    """
    context = browser.new_context(
        accept_downloads=True,
        viewport={"width": 1920, "height": 1080},
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        locale="id-ID",
    )
    context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )

    pages = {name: context.new_page() for name in urls}
    dfs: dict[str, pd.DataFrame] = {}
    page_values: dict[str, str] = {}

    # Navigasi semua tab dulu (requests jalan paralel di sisi network)
    for name, url in urls.items():
        logger.info("[%s] Membuka tab → %s", name, url)
        pages[name].goto(url)

    # Tunggu semua tab selesai load
    for name, page in pages.items():
        page.wait_for_load_state("networkidle")

    # Download dari masing-masing tab
    for name, page in pages.items():
        df, pv = download_df(page, urls[name], name, max_retry, logger)
        if df is None:
            context.close()
            return {}, {}
        dfs[name] = df
        page_values[name] = pv

    context.close()
    return dfs, page_values


# ---------------------------------------------------------------------------
# Utilitas
# ---------------------------------------------------------------------------

def log_count(name: str, page_value: str, df_count: int, logger: logging.Logger) -> None:
    normalized = page_value.replace(".", "").replace(",", "").replace(" ", "").strip()
    status = "✅ SAMA" if normalized == str(df_count) else "⚠️  BEDA"
    logger.info("[%s] Halaman: %s | Data: %d | %s", name, normalized, df_count, status)


def safe_json(data) -> str:
    raw = _json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    return raw.replace("</", "<\\/")


# ---------------------------------------------------------------------------
# Merge & analisis
# ---------------------------------------------------------------------------

def merge_rup_realisasi(
    df_rup: pd.DataFrame,
    df_realisasi: pd.DataFrame,
    logger: logging.Logger,
) -> pd.DataFrame:
    df_rup = df_rup.copy()
    df_realisasi = df_realisasi.copy()

    df_rup["Kode RUP"] = df_rup["Kode RUP"].astype(str).str.strip()
    df_realisasi["Kode RUP"] = df_realisasi["Kode RUP"].astype(str).str.strip()

    agg_spec: dict = {"Total Nilai (Rp)": "sum"}
    if "Nama Penyedia" in df_realisasi.columns:
        agg_spec["Nama Penyedia"] = lambda s: " | ".join(
            sorted({str(v).strip() for v in s if pd.notna(v) and str(v).strip()})
        )
    realisasi_agg = (
        df_realisasi
        .groupby("Kode RUP", as_index=False)
        .agg(agg_spec)
        .rename(columns={"Total Nilai (Rp)": "Total Realisasi (Rp)"})
    )

    merged = df_rup.merge(realisasi_agg, on="Kode RUP", how="outer", indicator=True)

    anomaly_mask = merged["_merge"] == "right_only"
    merged.loc[anomaly_mask, "Nama Paket"] = "[Tidak ada data RUP]"
    merged["Total Nilai (Rp)"]     = merged["Total Nilai (Rp)"].fillna(0)
    merged["Total Realisasi (Rp)"] = merged["Total Realisasi (Rp)"].fillna(0)
    if "Nama Penyedia" not in merged.columns:
        merged["Nama Penyedia"] = ""
    merged["Nama Penyedia"] = merged["Nama Penyedia"].fillna("")
    merged.drop(columns=["_merge"], inplace=True)

    pct = (
        merged["Total Realisasi (Rp)"]
        / merged["Total Nilai (Rp)"].replace(0, float("nan"))
        * 100
    )
    merged["Status"] = pct.apply(
        lambda x: "" if (x is None or (isinstance(x, float) and math.isnan(x))) else f"{x:.2f}%"
    )
    merged.loc[anomaly_mask, "Status"] = "Realisasi tanpa RUP"

    logger.info("Kode RUP di realisasi tanpa pasangan RUP: %d", anomaly_mask.sum())
    return merged


# ---------------------------------------------------------------------------
# Render HTML
# ---------------------------------------------------------------------------

def render_html(
    merged: pd.DataFrame,
    total_rup: int,
    total_realisasi: int,
    cfg: argparse.Namespace,
) -> str:
    anomaly_count = int((merged["Status"] == "Realisasi tanpa RUP").sum())

    cols = ["Kode RUP", "Nama Paket", "Total Nilai (Rp)", "Total Realisasi (Rp)", "Nama Penyedia", "Status"]
    for c in cols:
        if c not in merged.columns:
            merged[c] = ""

    records = merged[cols].fillna("").values.tolist()
    data_records = [
        [str(r[0]), str(r[1]), float(r[2]), float(r[3]), str(r[4]), str(r[5])]
        for r in records
    ]
    data_json = safe_json(data_records)
    generated_at = datetime.now().strftime("%d %B %Y, %H:%M:%S")

    return f"""<!DOCTYPE html>
<html lang="id">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Laporan RUP vs Realisasi - {cfg.instansi} ({cfg.tahun})</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{ margin: 24px; font: 13px Arial, sans-serif; color: #222; background: #fff; }}
  h1 {{ margin-bottom: 4px; }} .meta {{ color: #666; margin: 0 0 20px; font-size: 12px; }}
  .cards {{ display: grid; gap: 16px; margin-bottom: 24px;
            grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); }}
  .card, .table-container {{ border: 1px solid #eee; border-radius: 8px; background: #fff; }}
  .card {{ padding: 16px; }}
  .card p {{ margin: 0; }}
  .label {{ color: #777; margin-bottom: 4px !important; }}
  .value {{ font-size: 24px; font-weight: 600; }}
  .danger {{ color: #c0392b; }}
  .table-toolbar {{ margin-bottom: 12px; }}
  .table-toolbar input {{ width: 100%; padding: 10px 12px; border: 1px solid #ddd;
                          border-radius: 8px; font: inherit; }}
  .table-container {{ max-height: 600px; overflow-y: auto; }}
  table {{ width: 100%; border-collapse: collapse; }}
  th, td {{ padding: 8px; text-align: left; }}
  td {{ border-top: 1px solid #eee; }}
  th {{ position: sticky; top: 0; background: #f5f5f5; cursor: pointer; user-select: none; }}
  th.sortable::after {{ content: " △▽"; color: #999; font-size: 11px; white-space: pre; }}
  th.sort-asc::after  {{ content: " △"; color: #222; }}
  th.sort-desc::after {{ content: " ▽"; color: #222; }}
  tr.anomaly {{ background: #fcebeb; }}
  #rowCount {{ font-size: 12px; color: #666; margin-bottom: 6px; }}
</style>
</head>
<body>
  <h1>Laporan RUP vs Realisasi</h1>
  <p class="meta">
    Instansi: <strong>{cfg.instansi}</strong> &nbsp;|&nbsp;
    Jenis KLPD: <strong>{cfg.jenis_klpd}</strong> &nbsp;|&nbsp;
    Tahun: <strong>{cfg.tahun}</strong> &nbsp;|&nbsp;
    Dibuat: {generated_at}
  </p>
  <div class="cards">
    <div class="card"><p class="label">Total RUP</p><p class="value">{total_rup}</p></div>
    <div class="card"><p class="label">Total Realisasi</p><p class="value">{total_realisasi}</p></div>
    <div class="card"><p class="label">Realisasi tanpa RUP</p>
      <p class="value danger">{anomaly_count}</p></div>
  </div>
  <h2>Detail Data</h2>
  <div class="table-toolbar">
    <input id="tableSearch" type="text"
           placeholder="Cari Kode RUP, Nama Paket, atau Nama Penyedia…">
  </div>
  <div id="rowCount"></div>
  <div class="table-container">
    <table id="detailTable">
      <thead>
        <tr>
          <th class="sortable" data-type="text">Kode RUP</th>
          <th class="sortable" data-type="text">Nama Paket</th>
          <th class="sortable" data-type="number" style="text-align:right">Total Perencanaan (Rp)</th>
          <th class="sortable" data-type="number" style="text-align:right">Total Realisasi (Rp)</th>
          <th class="sortable" data-type="text">Nama Penyedia</th>
          <th class="sortable" data-type="text">Status</th>
        </tr>
      </thead>
      <tbody id="tbody"></tbody>
    </table>
  </div>
<script>
const DATA_ALL = {data_json};
let DATA = DATA_ALL.slice();
const tbody = document.getElementById('tbody');
const headers = document.querySelectorAll('th.sortable');
const searchInput = document.getElementById('tableSearch');
const rowCount = document.getElementById('rowCount');

function fmtRupiah(n) {{ return Math.round(n).toLocaleString('id-ID'); }}
function esc(s) {{
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}}

function render(rows) {{
  tbody.innerHTML = rows.map(r =>
    `<tr${{r[5]==='Realisasi tanpa RUP' ? ' class="anomaly"' : ''}}>
      <td>${{esc(r[0])}}</td><td>${{esc(r[1])}}</td>
      <td style="text-align:right">${{fmtRupiah(r[2])}}</td>
      <td style="text-align:right">${{fmtRupiah(r[3])}}</td>
      <td>${{esc(r[4])}}</td><td>${{esc(r[5])}}</td>
    </tr>`
  ).join('');
  rowCount.textContent = 'Menampilkan ' + rows.length + ' dari ' + DATA_ALL.length + ' baris';
}}

let searchTimer;
searchInput.addEventListener('input', () => {{
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {{
    const q = searchInput.value.trim().toLowerCase();
    DATA = q
      ? DATA_ALL.filter(r => (r[0]+' '+r[1]+' '+r[4]).toLowerCase().includes(q))
      : DATA_ALL.slice();
    render(DATA);
  }}, 150);
}});

headers.forEach((header, index) => {{
  header.addEventListener('click', () => {{
    const dir = header.classList.contains('sort-asc') ? -1 : 1;
    const type = header.dataset.type || 'text';
    headers.forEach(th => th.classList.remove('sort-asc','sort-desc'));
    header.classList.add(dir === 1 ? 'sort-asc' : 'sort-desc');
    DATA.sort((a, b) => {{
      const x = a[index], y = b[index];
      if (type === 'number') return (x - y) * dir;
      return String(x).toLowerCase() < String(y).toLowerCase() ? -dir : dir;
    }});
    render(DATA);
  }});
}});

render(DATA);
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    cfg = parse_args()
    logger = setup_logging()
    logger.info(
        "Konfigurasi — tahun=%s, jenis_klpd=%s, instansi=%s, headless=%s, max_retry=%d",
        cfg.tahun, cfg.jenis_klpd, cfg.instansi, cfg.headless, cfg.max_retry,
    )

    urls = {
        "rup": (
            f"https://data.inaproc.id/rup"
            f"?tahun={cfg.tahun}&jenis_klpd={cfg.jenis_klpd}&instansi={cfg.instansi}"
        ),
        "realisasi": (
            f"https://data.inaproc.id/realisasi"
            f"?tahun={cfg.tahun}&jenis_klpd={cfg.jenis_klpd}&instansi={cfg.instansi}"
        ),
    }

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(
                headless=cfg.headless,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-infobars",
                    "--window-size=1920,1080",
                ],
            )
        except Exception as exc:
            if "Executable doesn't exist" in str(exc) or "chromium" in str(exc).lower():
                logger.error("Chromium belum diinstall. Jalankan: playwright install chromium")
                sys.exit(1)
            raise

        dfs, page_values = download_parallel(browser, urls, cfg.max_retry, logger)
        browser.close()

    if not dfs:
        logger.error("Download gagal — proses dihentikan.")
        sys.exit(1)

    log_count("rup",       page_values["rup"],       len(dfs["rup"]),       logger)
    log_count("realisasi", page_values["realisasi"], len(dfs["realisasi"]), logger)

    merged = merge_rup_realisasi(dfs["rup"], dfs["realisasi"], logger)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    basename  = f"rup_vs_realisasi_{cfg.instansi}_{cfg.tahun}_{timestamp}"

    csv_path  = Path(f"{basename}.csv")
    html_path = Path(f"{basename}.html")

    try:
        merged.to_csv(csv_path, index=False, encoding="utf-8-sig", sep=";")
        logger.info("✅ CSV disimpan ke: %s", csv_path)
    except Exception as exc:
        logger.warning("Gagal menyimpan CSV: %s", exc)

    html_path.write_text(
        render_html(merged, len(dfs["rup"]), len(dfs["realisasi"]), cfg),
        encoding="utf-8",
    )
    logger.info("✅ HTML disimpan ke: %s", html_path)


if __name__ == "__main__":
    main()