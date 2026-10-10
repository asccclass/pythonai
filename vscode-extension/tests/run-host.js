const path=require('path');
const fs=require('fs');
const { runTests }=require('@vscode/test-electron');
const root=path.resolve(__dirname,'..');
const cache=path.resolve(root,'.vscode-test');fs.mkdirSync(cache,{recursive:true});
runTests({extensionDevelopmentPath:root,extensionTestsPath:path.join(__dirname,'host'),
    launchArgs:['--disable-extensions','--disable-gpu','--skip-welcome','--skip-release-notes',
        '--user-data-dir='+path.join(cache,'profile'),'--extensions-dir='+path.join(cache,'extensions')]
}).catch(error=>{console.error(error);process.exit(1);});
