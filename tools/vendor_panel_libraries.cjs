// Regenerate committed browser assets from package-lock.json with npm ci && npm run vendor:panels.
const fs = require('node:fs');
const path = require('node:path');
const root = path.resolve(__dirname, '..');
const files = {
  dompurify: {'dist/purify.min.js': 'purify.min.js', LICENSE: 'LICENSE', 'LICENSE-MPL': 'LICENSE-MPL'},
  marked: {'lib/marked.umd.js': 'marked.umd.js', LICENSE: 'LICENSE'},
};
for (const [pkg, entries] of Object.entries(files)) {
  const dest = path.join(root, 'web/static/vendor', pkg);
  fs.mkdirSync(dest, {recursive: true});
  for (const [source, name] of Object.entries(entries)) {
    const input = path.join(root, 'node_modules', pkg, source);
    const output = path.join(dest, name);
    if (name.startsWith('LICENSE')) {
      // Normalize trailing spaces in license text without changing its terms.
      fs.writeFileSync(output, fs.readFileSync(input, 'utf8').replace(/[ \t]+$/gm, ''));
    } else {
      fs.copyFileSync(input, output);
    }
  }
}
