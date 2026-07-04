// Node.js WebAssembly Instantiation and Runner

const fs = require('fs');

if (process.argv.length < 3) {
    console.log("Usage: node run_wasm.js <compiled_file.wasm> [args...]");
    process.exit(1);
}

const wasmPath = process.argv[2];
if (!fs.existsSync(wasmPath)) {
    console.error(`Error: WebAssembly binary file '${wasmPath}' not found`);
    process.exit(1);
}

const wasmBuffer = fs.readFileSync(wasmPath);

// Define environment imports matching the ones declared in the emitter
const imports = {
    env: {
        print_int: function(val) {
            console.log("Print output:", val);
        }
    }
};

WebAssembly.instantiate(wasmBuffer, imports).then(result => {
    const exports = result.instance.exports;
    
    if (exports.main) {
        const args = process.argv.slice(3).map(Number);
        
        const metadataPath = wasmPath + ".metadata.json";
        if (fs.existsSync(metadataPath)) {
            const metadata = JSON.parse(fs.readFileSync(metadataPath, 'utf8'));
            for (let i = 0; i < metadata.length; i++) {
                const param = metadata[i];
                if (i < args.length) {
                    const argVal = args[i];
                    if (param.kind === "typedef" || param.kind === "inline") {
                        const checkCode = `(function() {
                            var ${param.constraint_var} = ${argVal};
                            return ${param.constraint_str};
                        })()`;
                        try {
                            const passed = eval(checkCode);
                            if (!passed) {
                                if (param.kind === "typedef") {
                                    console.error(`expected ${param.name} to be a ${param.type_name}`);
                                } else {
                                    let constraintStr = param.constraint_str;
                                    if (constraintStr.startsWith('(') && constraintStr.endsWith(')')) {
                                        constraintStr = constraintStr.slice(1, -1);
                                    }
                                    console.error(`expected ${param.name} to be of type ${param.base_type}(${param.constraint_var}) where ${constraintStr}`);
                                }
                                process.exit(1);
                            }
                        } catch (e) {
                            // fallback
                        }
                    }
                }
            }
        }
        
        console.log(`Running main(...) with arguments: [${args.join(', ')}]`);
        const start = Date.now();
        const ret = exports.main(...args);
        const elapsed = Date.now() - start;
        console.log(`Execution return value: ${ret} (completed in ${elapsed}ms)`);
    } else if (exports.app_entry) {
        console.log("Running app_entry()...");
        const start = Date.now();
        exports.app_entry();
        const elapsed = Date.now() - start;
        console.log(`Execution finished (completed in ${elapsed}ms)`);
    } else {
        console.log("WASM loaded, but found no entry point 'main' or 'app_entry'.");
    }
}).catch(err => {
    console.error("WebAssembly Instantiation Failed:", err);
    process.exit(1);
});
