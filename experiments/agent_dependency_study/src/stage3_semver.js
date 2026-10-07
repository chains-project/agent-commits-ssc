// Fixed offline npm semantics. No npm install or network access.
const semver = require('../vendor/semver');
const readline = require('readline');
readline.createInterface({input:process.stdin}).on('line', line => {
  const q = JSON.parse(line);
  const valid = q.kind === 'exact' ? semver.valid(q.query) : semver.validRange(q.query);
  const matches = [], unknown = [];
  q.versions.forEach((v, i) => {
    if (!semver.valid(v)) { if(q.kind !== 'exact') unknown.push(i); }
    else if (valid && (q.kind === 'exact' ? semver.eq(v, q.query) : semver.satisfies(v, q.query))) matches.push(i);
  });
  process.stdout.write(JSON.stringify({valid:!!valid,matches,unknown})+'\n');
});
