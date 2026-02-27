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
    python map2shinua.py map.txt --props-file props.json
    python map2shinua.py map.txt --props צבע גודל
    cat map.txt | python map2shinua.py -
    python map2shinua.py map.txt -o import.txt

Options:
    --props-file FILE
        JSON file describing properties and their allowed values.
        Supports two formats:

        Array format (recommended — preserves order, includes options):
            [
              {"name": "צבע", "options": ["כחול", "אדום", "ירוק"]},
              {"name": "גודל", "options": ["גדול", "קטן"]}
            ]

        Dict format (shorthand):
            {"צבע": ["כחול", "אדום"], "גודל": ["גדול", "קטן"]}

        With --props-file, property values in the map are matched
        order-independently and case-insensitively (useful for English
        values like "Blue" / "blue" / "BLUE").

    --props NAME [NAME ...]
        Simple positional fallback: property names in the order the
        values appear in the map.  No options list — values are taken
        as-is.  Example: if carts show "כחול גדול", use --props צבע גודל
        Without either option the raw prop string is ignored.

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

def load_prop_defs(path: str) -> list[dict]:
    """Load property definitions from a JSON file.

    Accepts either:
      - Array:  [{"name": "צבע", "options": ["כחול", "אדום"]}, ...]
      - Dict:   {"צבע": ["כחול", "אדום"], ...}

    Returns a list of {"name": str, "options": [str, ...]} dicts.
    """
    with open(path, encoding='utf-8') as f:
        data = json.load(f)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return [{'name': k, 'options': v} for k, v in data.items()]
    raise ValueError('props.json must be a JSON array or object')


def match_props(raw: str, prop_defs: list[dict]) -> dict:
    """Map a raw value string to {propName: value}.

    Two modes, selected automatically:

    • **Order-independent** (when any prop has a non-empty options list):
      Each whitespace-separated token in *raw* is compared case-insensitively
      against every property's options list.  The first matching (prop, option)
      pair wins; a property is assigned at most once.  Tokens that match no
      option are silently discarded.  The canonical spelling from the options
      list is used in the output.

    • **Positional** (when all props have empty options lists, i.e. bare
      ``--props`` names were supplied without a props file):
      Values are assigned left-to-right in definition order; missing values
      get an empty string, extra values are discarded.

    Returns {} when prop_defs is empty.
    """
    if not prop_defs:
        return {}
    tokens = raw.split() if raw else []
    # Positional fallback: no prop has a defined options list
    if not any(p.get('options') for p in prop_defs):
        return {p['name']: (tokens[i] if i < len(tokens) else '')
                for i, p in enumerate(prop_defs)}
    # Order-independent matching
    result: dict = {}
    assigned: set = set()
    for token in tokens:
        tk = token.lower()
        for prop in prop_defs:
            name = prop['name']
            if name in assigned:
                continue
            canonical = next(
                (o for o in prop.get('options', []) if o.lower() == tk),
                None,
            )
            if canonical is not None:
                result[name] = canonical
                assigned.add(name)
                break
    return result


def build_db(locations: list[tuple[str, list[dict]]], prop_defs: list[dict]) -> dict:
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
            props = match_props(entry['rawProps'], prop_defs)

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

    return {
        'metadata': {
            'carts':      cart_id_list,
            'cartTypes':  [],
            'properties': prop_defs,
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
        '--props-file', metavar='FILE',
        help=(
            'JSON file with property definitions (name + options). '
            'Values in the map are matched order-independently and '
            'case-insensitively. '
            'Array format: [{"name":"צבע","options":["כחול","אדום"]},...] '
            'or dict format: {"צבע":["כחול","אדום"],...}'
        ),
    )
    parser.add_argument(
        '--props', nargs='*', default=[], metavar='NAME',
        help=(
            'Simple positional fallback: property names in the order they '
            'appear after the item count. Example: --props צבע גודל. '
            'Ignored when --props-file is given.'
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

    # ── resolve prop definitions ──
    if args.props_file:
        try:
            prop_defs = load_prop_defs(args.props_file)
        except FileNotFoundError:
            print(f"error: props file not found: {args.props_file}", file=sys.stderr)
            sys.exit(1)
        except (json.JSONDecodeError, ValueError) as exc:
            print(f"error: invalid props file: {exc}", file=sys.stderr)
            sys.exit(1)
        print(
            f"props file: {len(prop_defs)} propert{'y' if len(prop_defs)==1 else 'ies'} "
            f"({', '.join(p['name'] for p in prop_defs)})",
            file=sys.stderr,
        )
    elif args.props:
        # positional names only — no options, positional matching still works
        # via match_props but since options=[] no token will ever match;
        # use a simple positional fallback instead
        prop_defs = [{'name': n, 'options': []} for n in args.props]
        print(f"property names (positional): {args.props}", file=sys.stderr)
    else:
        prop_defs = []
        print(
            "no --props-file or --props given: property values in the map "
            "will be ignored.",
            file=sys.stderr,
        )

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

    # ── build + encode ──
    db = build_db(locations, prop_defs)
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
