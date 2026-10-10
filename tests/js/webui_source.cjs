const fs = require('node:fs');
const path = require('node:path');

function managerScript(webui) {
  const html = fs.readFileSync(path.join(webui, 'index.html'), 'utf8');
  return [...html.matchAll(/<script\s+src="([^"]+)"/g)].map((match) => {
    const relative = match[1].split('?')[0].replace(/^\//, '');
    const file = path.join(webui, relative);
    return fs.readFileSync(file, 'utf8');
  }).join('\n');
}

module.exports = {managerScript};
