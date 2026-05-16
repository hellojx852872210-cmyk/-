from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from .dedup import deduplicate_rows
from .service import collect_from_imeis, collect_label_pool_for_imeis


def _extract_imeis_from_file(path: Path, limit: int) -> list[str]:
    text = str(path).lower()
    if text.endswith('.xlsx') or text.endswith('.xls'):
        df = pd.read_excel(path)
    else:
        df = pd.read_csv(path)

    imei_col = None
    for c in df.columns:
        if str(c).strip().lower() == 'imei' or str(c).strip() == 'IMEI':
            imei_col = c
            break
    if imei_col is None:
        raise ValueError('输入文件缺少 IMEI 列')

    imeis: list[str] = []
    seen: set[str] = set()
    for raw in df[imei_col].tolist():
        digits = ''.join(ch for ch in str(raw).strip() if ch.isdigit())
        if len(digits) != 15:
            continue
        if digits in seen:
            continue
        seen.add(digits)
        imeis.append(digits)
        if limit > 0 and len(imeis) >= limit:
            break
    return imeis


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Standalone post-QC posterior extractor v2 (IMEI-only)')
    sub = parser.add_subparsers(dest='command')

    sample = sub.add_parser('sample-from-excel', help='Extract by IMEIs from excel/csv and export raw+dedup+stats')
    sample.add_argument('--input', required=True, help='Excel/CSV path containing IMEI column')
    sample.add_argument('--limit', type=int, default=300, help='Max unique IMEIs to process (0 means all)')
    sample.add_argument('--output-prefix', default='post_qc_posterior_v2', help='Output file prefix under runtime/')

    pool = sub.add_parser('pool-from-excel', help='Only fetch POST_QC_SUB_INTERCEPT pools(80/100/1) and export IMEI hit table')
    pool.add_argument('--input', required=True, help='Excel/CSV path containing IMEI column')
    pool.add_argument('--limit', type=int, default=0, help='Max unique IMEIs to process (0 means all)')
    pool.add_argument('--page-size', type=int, default=50, help='Page size for merchantProductList')
    pool.add_argument('--max-pages', type=int, default=3, help='Max pages per status per account for quick validation')
    pool.add_argument('--output-prefix', default='post_qc_pool_check', help='Output file prefix under runtime/')

    return parser


def _run_sample_from_excel(args: argparse.Namespace) -> int:
    input_path = Path(args.input).expanduser().resolve()
    if not input_path.exists():
        raise FileNotFoundError(f'输入文件不存在: {input_path}')

    imeis = _extract_imeis_from_file(input_path, int(args.limit or 0))
    rows, stats = collect_from_imeis(imeis)
    dedup_rows = deduplicate_rows(rows)

    runtime = Path(__file__).resolve().parents[2] / 'runtime'
    runtime.mkdir(parents=True, exist_ok=True)
    prefix = str(args.output_prefix or 'post_qc_posterior_v2').strip() or 'post_qc_posterior_v2'

    raw_path = runtime / f'{prefix}_raw.csv'
    dedup_path = runtime / f'{prefix}_dedup.csv'
    stats_path = runtime / f'{prefix}_stats.json'

    raw_df = pd.DataFrame(rows)
    dedup_df = pd.DataFrame(dedup_rows)
    if not raw_df.empty:
        raw_df.insert(0, 'row_id', range(1, len(raw_df) + 1))
    if not dedup_df.empty:
        dedup_df.insert(0, 'row_id', range(1, len(dedup_df) + 1))

    raw_df.to_csv(raw_path, index=False, encoding='utf-8-sig')
    dedup_df.to_csv(dedup_path, index=False, encoding='utf-8-sig')
    stats_path.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding='utf-8')

    print(f'RAW={raw_path}')
    print(f'DEDUP={dedup_path}')
    print(f'STATS={stats_path}')
    print(f'RAW_ROWS={len(raw_df)}')
    print(f'DEDUP_ROWS={len(dedup_df)}')
    print('IMEI_DISTRIBUTION=' + json.dumps(stats.get('imei_distribution') or {}, ensure_ascii=False))
    return 0


def _run_pool_from_excel(args: argparse.Namespace) -> int:
    input_path = Path(args.input).expanduser().resolve()
    if not input_path.exists():
        raise FileNotFoundError(f'输入文件不存在: {input_path}')

    imeis = _extract_imeis_from_file(input_path, int(args.limit or 0))
    rows, stats = collect_label_pool_for_imeis(
        imeis=imeis,
        statuses=("80", "100", "1"),
        page_size=int(args.page_size or 50),
        max_pages=int(args.max_pages or 3),
    )

    runtime = Path(__file__).resolve().parents[2] / 'runtime'
    runtime.mkdir(parents=True, exist_ok=True)
    prefix = str(args.output_prefix or 'post_qc_pool_check').strip() or 'post_qc_pool_check'

    hits_path = runtime / f'{prefix}_hits.csv'
    stats_path = runtime / f'{prefix}_stats.json'

    hit_df = pd.DataFrame(rows)
    if not hit_df.empty:
        hit_df.insert(0, 'row_id', range(1, len(hit_df) + 1))
    hit_df.to_csv(hits_path, index=False, encoding='utf-8-sig')
    stats_path.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding='utf-8')

    print(f'HITS={hits_path}')
    print(f'STATS={stats_path}')
    print(f'HIT_ROWS={len(hit_df)}')
    return 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.command == 'sample-from-excel':
        return _run_sample_from_excel(args)
    if args.command == 'pool-from-excel':
        return _run_pool_from_excel(args)
    parser.print_help()
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
