// Configure a single-member replica set so Questly can use MongoDB transactions.
const admin = db.getSiblingDB("admin");
let configured = false;
try {
  const status = admin.runCommand({ replSetGetStatus: 1 });
  configured = status.ok === 1;
} catch (_) {
  configured = false;
}

if (!configured) {
  const result = admin.runCommand({
    replSetInitiate: {
      _id: "rs0",
      members: [{ _id: 0, host: "mongo:27017" }],
    },
  });
  if (result.ok !== 1 && result.codeName !== "AlreadyInitialized") {
    throw new Error(`Replica set initialization failed: ${tojson(result)}`);
  }
}

let primary = false;
for (let attempt = 0; attempt < 30; attempt += 1) {
  try {
    primary = admin.runCommand({ replSetGetStatus: 1 }).myState === 1;
  } catch (_) {
    primary = false;
  }
  if (primary) break;
  sleep(1000);
}
if (!primary) throw new Error("MongoDB replica set did not elect its primary.");
print("MongoDB replica set rs0 is ready.");
