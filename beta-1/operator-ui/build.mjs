// SPDX-License-Identifier: AGPL-3.0-only
import {createRequire} from 'node:module';
import {pathToFileURL,fileURLToPath} from 'node:url';
import path from 'node:path';
const root=path.dirname(fileURLToPath(import.meta.url));
const dependencies=process.env.STATION_UI_DEPENDENCIES||path.join(root,'node_modules');
const require=createRequire(path.join(dependencies,'../package.json'));
const {build}=await import(pathToFileURL(path.join(dependencies,'vite/dist/node/index.js')).href);
await build({root,configFile:false,resolve:{alias:{'react-dom/client':require.resolve('react-dom/client'),'react':require.resolve('react')}},
 build:{outDir:path.join(root,'../townlight_station/ui'),emptyOutDir:true,cssCodeSplit:false,
 rollupOptions:{output:{entryFileNames:'operator.js',assetFileNames:asset=>asset.names.some(n=>n.endsWith('.css'))?'operator.css':'[name][extname]'}}}});
