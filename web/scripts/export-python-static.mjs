import { cp, mkdir, readFile, readdir, rename, rm, writeFile } from "node:fs/promises";
import { fileURLToPath, pathToFileURL } from "node:url";
import path from "node:path";

const webRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const distRoot = path.join(webRoot, "dist");
const clientRoot = path.join(distRoot, "client");
const clientAssetsRoot = path.join(clientRoot, "_next", "static");
const serverEntry = path.join(distRoot, "server", "index.js");
const outputRoot = path.resolve(webRoot, "..", "src", "dub_server", "web_static");
const stagingRoot = `${outputRoot}.staging-${process.pid}`;
const backupRoot = `${outputRoot}.backup-${process.pid}`;
const embeddedDeploymentVersion = "embedded-static-v1";

function stabilizeClientAsset(source) {
  return source.replace(/\/{0,2}_next\/static\/chunks\//g, "assets/");
}

function stabilizeRenderedHtml(html) {
  const deploymentVersionPattern = /\\\"deploymentVersion\\\":\\\"[^\"\\]+\\\"/g;
  const matches = html.match(deploymentVersionPattern) ?? [];
  if (matches.length !== 1) {
    throw new Error(
      `Expected one Vinext deploymentVersion in rendered HTML, found ${matches.length}`,
    );
  }
  const stableHtml = html.replace(
    deploymentVersionPattern,
    `\\\"deploymentVersion\\\":\\\"${embeddedDeploymentVersion}\\\"`,
  );
  return stableHtml.replace(
    /\/_next\/static\/(?:[^"'<>\s/]+\/)*([^"'<>\s/]+)/g,
    "/assets/$1",
  );
}

async function copyFlatAssets(source, destination, names = new Set()) {
  for (const entry of await readdir(source, { withFileTypes: true })) {
    const sourcePath = path.join(source, entry.name);
    if (entry.isDirectory()) {
      await copyFlatAssets(sourcePath, destination, names);
    } else if (entry.isFile()) {
      if (names.has(entry.name)) {
        throw new Error(`Trùng tên asset client khi flatten: ${entry.name}`);
      }
      names.add(entry.name);
      const destinationPath = path.join(destination, entry.name);
      if (entry.name.endsWith(".js")) {
        await writeFile(
          destinationPath,
          stabilizeClientAsset(await readFile(sourcePath, "utf8")),
          "utf8",
        );
      } else {
        await cp(sourcePath, destinationPath);
      }
    }
  }
}

async function renderIndex() {
  const workerUrl = pathToFileURL(serverEntry);
  workerUrl.searchParams.set("embed", `${process.pid}-${Date.now()}`);
  const { default: worker } = await import(workerUrl.href);
  const response = await worker.fetch(
    new Request("http://127.0.0.1:8080/", {
      headers: { accept: "text/html" },
    }),
    {
      ASSETS: {
        fetch: async () => new Response("Not found", { status: 404 }),
      },
    },
    {
      waitUntil() {},
      passThroughOnException() {},
    },
  );
  if (!response.ok) {
    throw new Error(`Không thể render dashboard: HTTP ${response.status}`);
  }
  const html = stabilizeRenderedHtml(await response.text());
  if (!html.includes('<html lang="vi">') || !html.includes("/assets/")) {
    throw new Error("HTML dashboard không có cấu trúc hoặc asset client mong đợi");
  }
  return html;
}

async function activateStaging() {
  let hasBackup = false;
  try {
    await rename(outputRoot, backupRoot);
    hasBackup = true;
  } catch (error) {
    if (error?.code !== "ENOENT") throw error;
  }

  try {
    await rename(stagingRoot, outputRoot);
  } catch (error) {
    if (hasBackup) await rename(backupRoot, outputRoot);
    throw error;
  }

  if (hasBackup) await rm(backupRoot, { recursive: true, force: true });
}

await rm(stagingRoot, { recursive: true, force: true });
await mkdir(stagingRoot, { recursive: true });

try {
  const html = await renderIndex();
  const stagingAssets = path.join(stagingRoot, "assets");
  await mkdir(stagingAssets);
  await copyFlatAssets(clientAssetsRoot, stagingAssets);
  await cp(path.join(clientRoot, "og.png"), path.join(stagingRoot, "og.png"));
  await writeFile(path.join(stagingRoot, "index.html"), html, "utf8");

  const copiedIndex = await readFile(path.join(stagingRoot, "index.html"), "utf8");
  if (copiedIndex !== html) throw new Error("Xác minh index.html sau khi ghi thất bại");

  await activateStaging();
  console.log(`Đã nhúng dashboard vào ${outputRoot}`);
} catch (error) {
  await rm(stagingRoot, { recursive: true, force: true });
  throw error;
}
