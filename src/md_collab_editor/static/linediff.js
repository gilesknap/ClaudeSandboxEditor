// Line diffs with diff_match_patch, for scm.js (diff tabs) and scm-worker.js (the gutter's change
// bars, off the main thread). Plain script after diff_match_patch.js; exposes `LineDiff`.
//
//   LineDiff.lineChunks(a, b) → [{origFrom, origTo, editFrom, editTo}]: runs of changed lines
//       (0-based, end exclusive), with line i of each side being CodeMirror's line i of that text

var LineDiff = (() => {
  function lineChunks(a, b) {
    if (a === b) return [];
    a += '\n';
    b += '\n';
    const dmp = new diff_match_patch();
    dmp.Diff_Timeout = 1;
    const x = dmp.diff_linesToChars_(a, b);
    if (x.lineArray.length > 65000) return coarseChunks(a, b);   // one char per line: 16 bits
    const chunks = [];
    let o = 0, e = 0, cur = null;
    for (const [op, s] of dmp.diff_main(x.chars1, x.chars2, false)) {
      const n = s.length;
      if (op === 0) {
        if (cur) { chunks.push(cur); cur = null; }
        o += n; e += n;
        continue;
      }
      if (!cur) cur = { origFrom: o, origTo: o, editFrom: e, editTo: e };
      if (op < 0) { o += n; cur.origTo = o; } else { e += n; cur.editTo = e; }
    }
    if (cur) chunks.push(cur);
    return chunks;
  }

  // a very large file: everything between the common first and last lines is one change
  function coarseChunks(a, b) {
    const A = a.split('\n'), B = b.split('\n');
    let p = 0;
    while (p < A.length && p < B.length && A[p] === B[p]) p++;
    let q = 0;
    while (q < A.length - p && q < B.length - p && A[A.length - 1 - q] === B[B.length - 1 - q]) q++;
    return p === A.length && p === B.length ? [] : [{ origFrom: p, origTo: A.length - q, editFrom: p, editTo: B.length - q }];
  }

  return { lineChunks };
})();
