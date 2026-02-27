#!/usr/bin/env python3
"""map2shinua.py — Convert a שינוע map text export to a shinua import file.

The map format is produced by the app's "יצירת מיפוי" button:

    מיפוי
    31.05.2025 14:30

    מיקום1:
    8: 5 כחול גדול
    12: empty
    [רצפה] 3 אדום קטן
    ----------------------------------
    מיקום2:
    15: 2 ירוק
    ----------------------------------

The output is the full import blob (SHINUA_DATA_START...SHINUA_DATA_END)
that can be pasted into the app's import screen.

Usage:
    python map2shinua.py map.txt
    python map2shinua.py map.txt --props צבע גודל
    cat map.txt | python map2shinua.py -
    python map2shinua.py map.txt -o import.txt

Options:
    --props NAME [NAME ...]
        Property names, in the order the values appear in the map.
        Example: if carts show "כחול גדול", use --props צבע גודל
        Without --props the raw prop string is ignored (properties left empty).
    -o / --output FILE
        Write to FILE instead of stdout.
"""

import argparse
import base64
import json
import re
import sys
import time
from datetime import datetime


# ── Map parser ────────────────────────────────────────────────────────────────

def parse_map(text: str) -> list[tuple[str, list[dict]]]:
    """Return [(location_name, [entry, ...]), ...].

    Each entry dict:
        type     : 'cart' | 'floor'
        cartId   : str   (cart only)
        itemCount: int
        isEmpty  : bool
        rawProps : str   (space-joined values, may be empty)
    """
    locations: list[tuple[str, list[dict]]] = []
    current_loc: str | None = None
    entries: list[dict] = []

    def flush():
        nonlocal current_loc, entries
        if current_loc is not None:
            locations.append((current_loc, entries))
        current_loc = None
        entries = []

    for raw in text.splitlines():
        line = raw.strip()

        # blank or header keywords
        if not line:
            continue
        if line == 'מיפוי':
            continue
        # date line  e.g. "31.05.2025 14:30"  or  "31/05/2025 14:30"
        if re.match(r'^\d{2}[./]\d{2}[./]\d{4}', line):
            continue
        # separator
        if line.startswith('---'):
            flush()
            continue

        # floor item line: "[רצפה] ..."
        if line.startswith('[רצפה]'):
            rest = line[len('[רצפה]'):].strip()
            if rest == 'empty':
                entries.append({'type': 'floor', 'itemCount': 0, 'isEmpty': True, 'rawProps': ''})
            else:
                parts = rest.split(None, 1)  # split off the count
                count = int(parts[0]) if parts and parts[0].isdigit() else 0
                raw_props = parts[1] if len(parts) > 1 else ''
                entries.append({'type': 'floor', 'itemCount': count,
                                'isEmpty': count == 0, 'rawProps': raw_props.strip()})
            continue

        # location header: ends with ':' and nothing follows (no space-separated count)
        # e.g. "מיקום1:" but NOT "8:" (cart line would be "8: 5 ..." with content after)
        if re.match(r'^[^:\[]+:$', line):
            flush()
            current_loc = line[:-1].strip()
            entries = []
            continue

        # cart line: "cartId: rest"
        m = re.match(r'^(.+?):\s*(.+)$', line)
        if m and current_loc is not None:
            cart_id = m.group(1).strip()
            rest = m.group(2).strip()
            if rest == 'empty':
                entries.append({'type': 'cart', 'cartId': cart_id,
                                'itemCount': 0, 'isEmpty': True, 'rawProps': ''})
            else:
                parts = rest.split(None, 1)
                count = int(parts[0]) if parts and parts[0].isdigit() else 0
                raw_props = parts[1] if len(parts) > 1 else ''
                entries.append({'type': 'cart', 'cartId': cart_id, 'itemCount': count,
                                'isEmpty': count == 0, 'rawProps': raw_props.strip()})
            continue

    # trailing block without a separator
    flush()
    return locations


# ── Build shinua DB dict ──────────────────────────────────────────────────────

def props_dict(raw: str, names: list[str]) -> dict:
    """Map a space-joined value string to {propName: value, ...}.

    If names is empty, returns {} (properties ignored).
    Extra values beyond len(names) are discarded; missing values get ''.
    """
    if not names:
        return {}
    values = raw.split() if raw else []
    return {name: (values[i] if i < len(values) else '') for i, name in enumerate(names)}


def build_db(locations: list[tuple[str, list[dict]]], prop_names: list[str]) -> dict:
    """Convert parsed locations → shinua DB dict."""
    now_ms = int(time.time() * 1000)
    fi_counter = [0]  # mutable counter for unique floor-item IDs

    carts: dict = {}
    floor_items: dict = {}
    cart_id_list: list[str] = []
    location_meta: list[dict] = []

    for loc_name, entries in locations:
        location_meta.append({'name': loc_name, 'inMap': True})

        for entry in entries:
            props = props_dict(entry['rawProps'], prop_names)

            if entry['type'] == 'cart':
                cid = entry['cartId']
                carts[cid] = {
                    'cartId':      cid,
                    'cartType':    '',
                    'location':    loc_name,
                    'subLocation': '',
                    'itemCount':   entry['itemCount'],
                    'itemIds':     [],
                    'properties':  props,
                    'isEmpty':     entry['isEmpty'],
                    'lastUpdated': now_ms,
                }
                if cid not in cart_id_list:
                    cart_id_list.append(cid)

            else:  # floor
                fi_id = f'fi_{now_ms + fi_counter[0]}'
                fi_counter[0] += 1
                floor_items[fi_id] = {
                    'id':          fi_id,
                    'location':    loc_name,
                    'subLocation': '',
                    'itemCount':   entry['itemCount'],
                    'itemIds':     [],
                    'properties':  props,
                    'isEmpty':     entry['isEmpty'],
                    'lastUpdated': now_ms,
                }

    # property metadata: names supplied by user, options unknown → []
    properties_meta = [{'name': n, 'options': []} for n in prop_names]

    return {
        'metadata': {
            'carts':      cart_id_list,
            'cartTypes':  [],
            'properties': properties_meta,
            'locations':  location_meta,
        },
        'carts':      carts,
        'floorItems': floor_items,
    }


# ── Encode to shinua import format ───────────────────────────────────────────

def encode_import(db: dict) -> str:
    """Return the full import text (header + SHINUA_DATA_START...END)."""
    json_str = json.dumps(db, ensure_ascii=False, separators=(',', ':'))
    # same as app's btoa(unescape(encodeURIComponent(str))) — UTF-8 base64
    b64 = base64.b64encode(json_str.encode('utf-8')).decode('ascii')

    # human-readable summary (mirrors app's generateExportText)
    loc_stats: dict[str, dict] = {}
    for cart in db['carts'].values():
        loc = cart.get('location') or 'ללא מיקום'
        s = loc_stats.setdefault(loc, {'carts': 0, 'empty': 0, 'fi': 0})
        s['carts'] += 1
        if cart.get('isEmpty'):
            s['empty'] += 1
    for fi in db['floorItems'].values():
        loc = fi.get('location') or 'ללא מיקום'
        s = loc_stats.setdefault(loc, {'carts': 0, 'empty': 0, 'fi': 0})
        s['fi'] += 1

    summary_lines = []
    for loc, s in loc_stats.items():
        parts = []
        if s['carts']:
            parts.append(f"{s['carts']} עגלות" + (f" ({s['empty']} ריקות)" if s['empty'] else ''))
        if s['fi']:
            parts.append(f"{s['fi']} קבוצות רצפה")
        summary_lines.append(f"{loc}: {', '.join(parts)}")

    now = datetime.now().strftime('%d.%m.%Y %H:%M')
    summary = '\n'.join(summary_lines)

    return (
        f"=== ייצוא נתונים — שינוע ===\n"
        f"תאריך: {now}\n\n"
        f"{summary}\n\n"
        f"SHINUA_DATA_START\n{b64}\nSHINUA_DATA_END"
    )


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        prog='map2shinua',
        description='Convert a שינוע map export to a shinua import blob.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  python map2shinua.py map.txt
  python map2shinua.py map.txt --props צבע גודל
  cat map.txt | python map2shinua.py -
  python map2shinua.py map.txt -o import.txt
""",
    )
    parser.add_argument(
        'input', nargs='?', default='-',
        help='Map text file path, or - to read from stdin (default: -)',
    )
    parser.add_argument(
        '--props', nargs='*', default=[], metavar='NAME',
        help=(
            'Property names in the order they appear after the item count. '
            'Example: --props צבע גודל'
        ),
    )
    parser.add_argument(
        '-o', '--output', default='-', metavar='FILE',
        help='Output file path (default: stdout)',
    )
    args = parser.parse_args()

    # ── read input ──
    if args.input == '-':
        text = sys.stdin.read()
    else:
        try:
            with open(args.input, encoding='utf-8') as f:
                text = f.read()
        except FileNotFoundError:
            print(f"error: file not found: {args.input}", file=sys.stderr)
            sys.exit(1)

    # ── parse ──
    locations = parse_map(text)
    if not locations:
        print('error: no location blocks found in the map.', file=sys.stderr)
        print('make sure the map was exported from שינוע (מיפוי button).', file=sys.stderr)
        sys.exit(1)

    cart_count  = sum(1 for _, es in locations for e in es if e['type'] == 'cart')
    floor_count = sum(1 for _, es in locations for e in es if e['type'] == 'floor')
    print(
        f"parsed {len(locations)} location(s), {cart_count} cart(s), {floor_count} floor item(s)",
        file=sys.stderr,
    )
    if args.props:
        print(f"property names: {args.props}", file=sys.stderr)
    else:
        print(
            "no --props given: property values in the map will be ignored. "
            "pass --props to map them to named properties.",
            file=sys.stderr,
        )

    # ── build + encode ──
    db = build_db(locations, args.props or [])
    output = encode_import(db)

    # ── write output ──
    if args.output == '-':
        print(output)
    else:
        with open(args.output, 'w', encoding='utf-8') as f:
            f.write(output)
        print(f"written to {args.output}", file=sys.stderr)


if __name__ == '__main__':
    main()
