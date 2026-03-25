#!/usr/bin/env node
/**
 * Round-trip test for shinua map import/export.
 * Extracts functions directly from index.html, runs a complex map through
 * parse → buildDB → generateMapText, and reports any data loss.
 */

const fs = require('fs');
const path = require('path');

// ── Extract and eval the functions from index.html ────────────────────────
const html = fs.readFileSync(path.join(__dirname, 'index.html'), 'utf8');
const lines = html.split('\n');

function extractLines(from, to) {
  return lines.slice(from - 1, to).join('\n');
}

// Line ranges of functions we need (from grep)
const code = [
  extractLines(1302, 1306),   // _floorKey
  extractLines(1799, 1808),   // formatDate
  extractLines(1844, 1970),   // _orderedPropValues + generateMapText
  extractLines(6377, 6775),   // _normalizeAliases, _parseMap, helpers, _buildMapDB
].join('\n');

// Eval in a sandbox that exposes the functions
const sandbox = {};
try {
  new Function(
    'sandbox',
    code + '\n' +
    'sandbox._floorKey = _floorKey;\n' +
    'sandbox._orderedPropValues = _orderedPropValues;\n' +
    'sandbox.generateMapText = generateMapText;\n' +
    'sandbox._normalizeAliases = _normalizeAliases;\n' +
    'sandbox._parseMap = _parseMap;\n' +
    'sandbox._buildMapDB = _buildMapDB;\n' +
    'sandbox._matchProps = _matchProps;\n'
  )(sandbox);
} catch (e) {
  console.error('Failed to extract functions from index.html:', e.message);
  process.exit(1);
}

const { _parseMap, _buildMapDB, generateMapText, _normalizeAliases } = sandbox;

// ── Test property definitions ─────────────────────────────────────────────
const propDefs = [
  { name: 'color',  options: ['אדום', 'כחול', 'ירוק', 'צהוב'] },
  { name: 'size',   options: ['קטן', 'גדול', 'בינוני'] },
  { name: 'grade',  options: ['A', 'B', 'C'] },
];

// ── Complex test map ──────────────────────────────────────────────────────
const TEST_MAP = `מיפוי 10:30
מחסן ראשי:
7-1(91): 5 אדום קטן A
7-2(91): 3 כחול גדול B
7-3(91): empty
===
מחסן ראשי:
תת-מיקום-צפון:
7-4(91): 2 ירוק בינוני C
7-4(91): 1 אדום גדול A
7-5(92): empty
פריטי רצפה:
אדום קטן - 10
כחול גדול - 5
ירוק - 3
===
מחסן שני:
משטח 1: 4 אדום קטן
משטח 1: 2 כחול גדול
משטח 2: empty
משטח #: 3 ירוק A
משטח #: 1 כחול B

משטח #: 5 אדום גדול
===
מחסן שני:
רצפה:
אדום גדול - 7
כחול קטן - 2
`;

// ── Run round-trip ────────────────────────────────────────────────────────
console.log('='.repeat(60));
console.log('ORIGINAL MAP');
console.log('='.repeat(60));
console.log(TEST_MAP);

const normalized = _normalizeAliases(TEST_MAP, propDefs);
const blocks = _parseMap(normalized);

console.log('='.repeat(60));
console.log(`PARSED: ${blocks.length} blocks`);
blocks.forEach((b, i) => {
  console.log(`  Block ${i+1}: location="${b.location}" sub="${b.subLocation}" entries=${b.entries.length}`);
  b.entries.forEach(e => {
    if (e.type === 'floor') {
      console.log(`    [floor] rawProps="${e.rawProps}" count=${e.itemCount}`);
    } else {
      const slots = e.slots.map(s => `count=${s.itemCount} props="${s.rawProps}"`).join(' | ');
      console.log(`    [cart]  id=${e.cartId} type=${e.cartType} ct=${e.containerType} slots: ${slots}`);
    }
  });
});

const { db: importedDb, unmatchedFloorLines } = _buildMapDB(blocks, propDefs);

console.log('\n' + '='.repeat(60));
console.log('BUILT DB');
console.log('='.repeat(60));
const carts = Object.values(importedDb.carts);
const fis = Object.values(importedDb.floorItems);
console.log(`  Carts: ${carts.length}`);
carts.forEach(c => {
  const extras = (c.extraItems || []).map(e => `count=${e.itemCount}`).join(', ');
  console.log(`    ${c.cartId}(${c.cartType||'-'}) @ ${c.location}/${c.subLocation} count=${c.itemCount} extras=[${extras}] empty=${c.isEmpty}`);
});
console.log(`  Floor items: ${fis.length}`);
fis.forEach(fi => {
  const props = Object.entries(fi.properties || {}).filter(([,v])=>v).map(([k,v])=>`${k}=${v}`).join(' ');
  console.log(`    @ ${fi.location}/${fi.subLocation} count=${fi.itemCount} props={${props}} empty=${fi.isEmpty}`);
});

if (unmatchedFloorLines.length) {
  console.log('\n⚠️  UNMATCHED FLOOR LINES:');
  unmatchedFloorLines.forEach(l => console.log('   ', l));
}

const exported = generateMapText(importedDb);
console.log('\n' + '='.repeat(60));
console.log('EXPORTED MAP');
console.log('='.repeat(60));
console.log(exported);

// ── Verification ──────────────────────────────────────────────────────────
console.log('='.repeat(60));
console.log('VERIFICATION');
console.log('='.repeat(60));

let ok = true;

// Check all input floor items appear in DB
const inputFloorPattern = /^(.+) - (\d+)$/gm;
let m;
const inputFloors = [];
while ((m = inputFloorPattern.exec(TEST_MAP)) !== null) {
  inputFloors.push({ props: m[1].trim(), count: parseInt(m[2]) });
}
console.log(`Input floor items: ${inputFloors.length}`);
console.log(`DB floor items:    ${fis.length}`);
if (inputFloors.length !== fis.length) {
  console.log(`  ❌ COUNT MISMATCH: expected ${inputFloors.length}, got ${fis.length}`);
  ok = false;
} else {
  console.log('  ✓ floor item count matches');
}

// Check export contains floor lines
const exportFloors = [];
while ((m = inputFloorPattern.exec(exported)) !== null) {
  exportFloors.push({ props: m[1].trim(), count: parseInt(m[2]) });
}
console.log(`Exported floor items: ${exportFloors.length}`);
if (exportFloors.length !== fis.length) {
  console.log(`  ❌ EXPORT COUNT MISMATCH: expected ${fis.length}, got ${exportFloors.length}`);
  ok = false;
} else {
  console.log('  ✓ exported floor item count matches');
}

// Check cart counts
const inputCartPattern = /^[^\s].+\([^)]+\):\s*(\d+)/gm;
let inputCartSlots = 0;
while ((m = inputCartPattern.exec(TEST_MAP)) !== null) inputCartSlots++;
const dbCartSlots = carts.reduce((s, c) => s + 1 + (c.extraItems||[]).length, 0);
console.log(`Input cart slot lines: ${inputCartSlots}`);
console.log(`DB cart slots (slot0 + extras): ${dbCartSlots}`);

console.log('\n' + (ok ? '✅ ALL CHECKS PASSED' : '❌ SOME CHECKS FAILED'));
