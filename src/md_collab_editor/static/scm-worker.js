// The gutter's change bars for scm.js, off the main thread: a line diff of a big file with
// thousands of changes takes diff_match_patch up to a second.
//   {init: {dmp, linediff}}  the scripts' URLs, once
//   {id, a, b}               → {id, chunks}: LineDiff.lineChunks(a, b), or null if it failed

self.onmessage = ({ data }) => {
  if (data.init) {
    importScripts(data.init.dmp, data.init.linediff);
    return;
  }
  let chunks = null;
  try { chunks = LineDiff.lineChunks(data.a, data.b); } catch {}
  self.postMessage({ id: data.id, chunks });
};
