import argparse
import json as _json
import os
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

# ===========================================================================
# ✏️  KONFIGURASI CEPAT — ubah di sini sesuai kebutuhan
# ===========================================================================
TAHUN      = "2026"
JENIS_KLPD = "3"
INSTANSI   = "D69"
HEADLESS   = True   # False = browser tampil; True = headless (pakai chromium args anti-deteksi)
MAX_RETRY  = 10
# ===========================================================================

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

class Logger:
    """Simple logger yang hanya print ke stdout."""
    def info(self, msg):    print(f"[INFO]  {msg}")
    def warning(self, msg): print(f"[WARN]  {msg}")
    def error(self, msg):   print(f"[ERROR] {msg}")

def setup_logging(*_) -> "Logger":
    return Logger()


# ---------------------------------------------------------------------------
# CLI / konfigurasi
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    """
    Prioritas konfigurasi (tertinggi ke terendah):
      1. Argumen CLI  (--tahun, --instansi, dst.)
      2. Environment variable (TAHUN, INSTANSI, dst.)
      3. Konstanta di blok KONFIGURASI CEPAT di atas
    """
    parser = argparse.ArgumentParser(
        description="Unduh RUP & Realisasi dari inaproc.id lalu buat laporan HTML."
    )
    parser.add_argument("--tahun",      default=os.getenv("TAHUN",      TAHUN))
    parser.add_argument("--jenis-klpd", default=os.getenv("JENIS_KLPD", JENIS_KLPD))
    parser.add_argument("--instansi",   default=os.getenv("INSTANSI",   INSTANSI))
    parser.add_argument("--max-retry",  type=int,
                        default=int(os.getenv("MAX_RETRY", str(MAX_RETRY))))
    parser.add_argument("--headless",   action=argparse.BooleanOptionalAction,
                        default=HEADLESS)  # ⚠️ ubah konstanta HEADLESS di atas, jangan True
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
# Download
# ---------------------------------------------------------------------------

def download_df(
    page,
    url: str,
    name: str,
    max_retry: int,
    logger,
) -> tuple[pd.DataFrame | None, str]:
    """
    Buka *url*, baca nilai statistik halaman, lalu unduh CSV.
    Return (DataFrame, page_value_string).
    Retry dengan backoff eksponensial; setiap exception di-log.
    """
    logger.info(f"[{name}] Membuka {url}")
    page.goto(url)
    page.wait_for_load_state("networkidle")

    try:
        page_value = page.locator(VALUE_SELECTOR).inner_text().strip()
        logger.info(f"[{name}] Nilai halaman: {page_value}")
    except Exception as exc:
        logger.warning(f"[{name}] Tidak bisa membaca nilai halaman: {exc}")
        page_value = "?"

    for attempt in range(1, max_retry + 1):
        logger.info(f"[{name}] Klik Download CSV (percobaan {attempt}/{max_retry})...")
        try:
            # Locate & scroll setiap attempt agar tidak stale
            btn = page.get_by_role("button", name="Download CSV")
            btn.wait_for(state="visible", timeout=30_000)
            btn.scroll_into_view_if_needed()

            with page.expect_download(timeout=60_000) as dl:
                btn.click()
            download = dl.value
            df = pd.read_csv(download.path(), encoding="utf-8-sig", sep=None, engine="python")
            logger.info(f"[{name}] ✅ Berhasil: {len(df)} baris, {len(df.columns)} kolom")
            _validate_columns(df, name, logger)
            return df, page_value

        except PlaywrightTimeout as exc:
            wait = min(5 * 2 ** (attempt - 1), 120)
            logger.warning(f"[{name}] Timeout percobaan {attempt}: {exc} — tunggu {wait}s")
            page.wait_for_timeout(wait * 1000)

        except Exception as exc:
            wait = min(5 * 2 ** (attempt - 1), 120)
            logger.error(f"[{name}] Error percobaan {attempt}: {type(exc).__name__}: {exc} — tunggu {wait}s")
            page.wait_for_timeout(wait * 1000)

    logger.error(f"[{name}] ❌ Gagal setelah {max_retry} percobaan.")
    return None, page_value


def _validate_columns(df: pd.DataFrame, name: str, logger: logging.Logger) -> None:
    required = REQUIRED_COLS.get(name, set())
    missing = required - set(df.columns)
    if missing:
        logger.warning(f"[{name}] Kolom wajib tidak ditemukan: {missing}")
        logger.warning(f"[{name}] Kolom yang tersedia: {list(df.columns)}")


# ---------------------------------------------------------------------------
# Utilitas
# ---------------------------------------------------------------------------

def log_count(name: str, page_value: str, df_count: int, logger: logging.Logger) -> None:
    normalized = page_value.replace(".", "").replace(",", "").replace(" ", "").strip()
    status = "✅ SAMA" if normalized == str(df_count) else "⚠️  BEDA"
    logger.info(f"[{name}] Halaman: {normalized} | Data: {df_count} | {status}")


def _s(v) -> str:
    return "" if pd.isna(v) else str(v)


def safe_json(data) -> str:
    """Serialize ke JSON dan escape '</script>' agar tidak membreak HTML."""
    raw = _json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    return raw.replace("</", "<\\/")


# ---------------------------------------------------------------------------
# Merge & analisis
# ---------------------------------------------------------------------------

def merge_rup_realisasi(
    df_rup: pd.DataFrame,
    df_realisasi: pd.DataFrame,
    logger,
) -> pd.DataFrame:
    # Normalkan kunci
    df_rup = df_rup.copy()
    df_realisasi = df_realisasi.copy()
    df_rup["Kode RUP"] = df_rup["Kode RUP"].astype(str).str.strip()
    df_realisasi["Kode RUP"] = df_realisasi["Kode RUP"].astype(str).str.strip()

    # Agregasi realisasi per Kode RUP
    group_cols: dict = {"Total Nilai (Rp)": "sum"}
    if "Nama Penyedia" in df_realisasi.columns:
        group_cols["Nama Penyedia"] = lambda s: " | ".join(
            sorted({str(v).strip() for v in s if pd.notna(v) and str(v).strip()})
        )

    realisasi_agg = df_realisasi.groupby("Kode RUP", as_index=False).agg(group_cols)
    realisasi_agg = realisasi_agg.rename(columns={"Total Nilai (Rp)": "Total Realisasi (Rp)"})

    # Merge
    merged = df_rup.merge(realisasi_agg, on="Kode RUP", how="left")
    merged["Total Realisasi (Rp)"] = merged["Total Realisasi (Rp)"].fillna(0)
    if "Nama Penyedia" not in merged.columns:
        merged["Nama Penyedia"] = ""
    merged["Nama Penyedia"] = merged["Nama Penyedia"].fillna("")
    if "Total Nilai (Rp)" in merged.columns:
        merged["Total Nilai (Rp)"] = merged["Total Nilai (Rp)"].fillna(0)

    # Realisasi yang tidak punya pasangan RUP
    missing_in_rup = realisasi_agg[
        ~realisasi_agg["Kode RUP"].isin(df_rup["Kode RUP"])
    ].copy()

    if not missing_in_rup.empty:
        extra = pd.DataFrame({col: [None] * len(missing_in_rup) for col in merged.columns})
        extra["Kode RUP"] = missing_in_rup["Kode RUP"].values
        extra["Total Realisasi (Rp)"] = missing_in_rup["Total Realisasi (Rp)"].values
        if "Nama Penyedia" in missing_in_rup.columns:
            extra["Nama Penyedia"] = missing_in_rup["Nama Penyedia"].values
        # Beri label eksplisit agar tidak membingungkan
        extra["Nama Paket"] = "[Tidak ada data RUP]"
        if "Total Nilai (Rp)" in extra.columns:
            extra["Total Nilai (Rp)"] = 0
        merged = pd.concat([merged, extra], ignore_index=True)

    missing_codes = set(missing_in_rup["Kode RUP"])
    merged["Status Data"] = merged["Kode RUP"].apply(
        lambda k: "Realisasi tanpa RUP" if k in missing_codes else ""
    )

    anomaly = (merged["Status Data"] == "Realisasi tanpa RUP").sum()
    logger.info(f"Kode RUP di realisasi tapi tidak ada di RUP: {anomaly}")
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
    anomaly_count = (merged["Status Data"] == "Realisasi tanpa RUP").sum()

    data_records = [
        [
            _s(row.get("Kode RUP", "")),
            _s(row.get("Nama Paket", "")),
            float(row.get("Total Nilai (Rp)", 0) or 0),
            float(row.get("Total Realisasi (Rp)", 0) or 0),
            _s(row.get("Nama Penyedia", "")),
            _s(row.get("Status Data", "")),
        ]
        for _, row in merged.iterrows()
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
          <th class="sortable" data-type="text">Status Data</th>
        </tr>
      </thead>
      <tbody id="tbody"></tbody>
    </table>
  </div>

<script>
const DATA_ALL = {data_json};
let DATA = DATA_ALL.slice();

const tbody      = document.getElementById('tbody');
const headers    = document.querySelectorAll('th.sortable');
const searchInput = document.getElementById('tableSearch');
const rowCount   = document.getElementById('rowCount');

function fmtRupiah(n) {{
  return Math.round(n).toLocaleString('id-ID');
}}

function escapeHtml(s) {{
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}}

function render(rows) {{
  const frag = document.createDocumentFragment();
  for (const r of rows) {{
    const tr = document.createElement('tr');
    if (r[5] === 'Realisasi tanpa RUP') tr.className = 'anomaly';
    tr.innerHTML =
      '<td>' + escapeHtml(r[0]) + '</td>' +
      '<td>' + escapeHtml(r[1]) + '</td>' +
      '<td style="text-align:right">' + fmtRupiah(r[2]) + '</td>' +
      '<td style="text-align:right">' + fmtRupiah(r[3]) + '</td>' +
      '<td>' + escapeHtml(r[4]) + '</td>' +
      '<td>' + escapeHtml(r[5]) + '</td>';
    frag.appendChild(tr);
  }}
  tbody.innerHTML = '';
  tbody.appendChild(frag);
  rowCount.textContent = 'Menampilkan ' + rows.length + ' dari ' + DATA_ALL.length + ' baris';
}}

searchInput.addEventListener('input', () => {{
  const q = searchInput.value.trim().toLowerCase();
  DATA = q
    ? DATA_ALL.filter(r => (r[0] + ' ' + r[1] + ' ' + r[4]).toLowerCase().includes(q))
    : DATA_ALL.slice();
  render(DATA);
}});

headers.forEach((header, index) => {{
  header.addEventListener('click', () => {{
    const dir = header.classList.contains('sort-asc') ? -1 : 1;
    const type = header.dataset.type || 'text';
    headers.forEach(th => th.classList.remove('sort-asc', 'sort-desc'));
    header.classList.add(dir === 1 ? 'sort-asc' : 'sort-desc');
    DATA.sort((a, b) => {{
      const x = a[index], y = b[index];
      if (type === 'number') return (x - y) * dir;
      const xs = String(x).toLowerCase(), ys = String(y).toLowerCase();
      return (xs < ys ? -1 : xs > ys ? 1 : 0) * dir;
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
        f"Konfigurasi — tahun={cfg.tahun}, jenis_klpd={cfg.jenis_klpd}, "
        f"instansi={cfg.instansi}, headless={cfg.headless}, max_retry={cfg.max_retry}"
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

    dfs: dict[str, pd.DataFrame] = {}
    page_values: dict[str, str] = {}

    with sync_playwright() as p:
        # Playwright otomatis tahu path Chromium-nya di tiap OS (Windows/Mac/Linux)
        # Kalau belum diinstall, jalankan: playwright install chromium
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
                print("[ERROR] Chromium belum diinstall. Jalankan: playwright install chromium")
                sys.exit(1)
            raise
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
        # Sembunyikan navigator.webdriver tanpa library tambahan
        context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        page = context.new_page()

        for name, url in urls.items():
            df, pv = download_df(page, url, name, cfg.max_retry, logger)
            if df is None:
                logger.error(f"[{name}] Gagal download — proses dihentikan.")
                browser.close()
                sys.exit(1)
            dfs[name] = df
            page_values[name] = pv

        browser.close()

    log_count("rup",       page_values["rup"],       len(dfs["rup"]),       logger)
    log_count("realisasi", page_values["realisasi"], len(dfs["realisasi"]), logger)

    merged = merge_rup_realisasi(dfs["rup"], dfs["realisasi"], logger)

    # Output dengan timestamp agar tidak overwrite
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = Path(".") / f"rup_vs_realisasi_{cfg.instansi}_{cfg.tahun}_{timestamp}.html"

    html = render_html(merged, len(dfs["rup"]), len(dfs["realisasi"]), cfg)

    out_path.write_text(html, encoding="utf-8")
    logger.info(f"✅ Laporan HTML disimpan ke: {out_path}")


if __name__ == "__main__":
    main()