import { readFile } from "node:fs/promises";
import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath } from "node:url";

const webRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const projectRoot = path.resolve(webRoot, "..");
const require = createRequire(import.meta.url);
const Ajv = require(
  path.join(webRoot, "node_modules", "ajv-formats", "node_modules", "ajv"),
).default;
const addFormats = require(path.join(webRoot, "node_modules", "ajv-formats")).default;
const schemaDirectory = process.argv[2] ? path.resolve(process.argv[2]) : null;
const schemaNames = [
  "bom-1.6.schema.json",
  "spdx.schema.json",
  "jsf-0.82.schema.json",
];

async function loadSchema(name) {
  if (schemaDirectory) {
    return JSON.parse(await readFile(path.join(schemaDirectory, name), "utf8"));
  }
  const url = `https://cyclonedx.org/schema/${name}`;
  let lastError;
  for (let attempt = 1; attempt <= 3; attempt += 1) {
    try {
      const response = await fetch(url, {
        redirect: "error",
        signal: AbortSignal.timeout(15_000),
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      return await response.json();
    } catch (error) {
      lastError = error;
      if (attempt < 3) {
        await new Promise((resolve) => setTimeout(resolve, attempt * 500));
      }
    }
  }
  throw new Error(`Không thể tải schema ${url}: ${lastError}`);
}

const [bomSchema, spdxSchema, jsfSchema] = await Promise.all(
  schemaNames.map(loadSchema),
);
const document = JSON.parse(
  await readFile(path.join(projectRoot, "release", "sbom.cdx.json"), "utf8"),
);
const ajv = new Ajv({ allErrors: true, strict: false });
addFormats(ajv);
ajv.addFormat("iri-reference", true);
ajv.addFormat("idn-email", true);
ajv.addSchema(spdxSchema);
ajv.addSchema(jsfSchema);
const validate = ajv.compile(bomSchema);
if (!validate(document)) {
  throw new Error(`SBOM không đạt CycloneDX 1.6:\n${JSON.stringify(validate.errors, null, 2)}`);
}
console.log("SBOM đạt schema CycloneDX 1.6 chính thức");
