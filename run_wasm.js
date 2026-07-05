// Node.js WebAssembly Instantiation and Runner

function loadMainMetadata(fs, wasmPath) {
    const metaPath = wasmPath + '.meta.json';
    if (!fs.existsSync(metaPath)) {
        return null;
    }
    return JSON.parse(fs.readFileSync(metaPath, 'utf8'));
}

function evalConstraintNode(node, value, varName) {
    if (!node) {
        return true;
    }
    if (node.kind === 'lit') {
        return node.value;
    }
    if (node.kind === 'id') {
        if (node.name === varName) {
            return value;
        }
        throw new Error(`Unknown identifier '${node.name}' in type constraint`);
    }
    if (node.kind === 'bin') {
        const left = evalConstraintNode(node.left, value, varName);
        const right = evalConstraintNode(node.right, value, varName);
        switch (node.op) {
            case '&&': return left && right;
            case '||': return left || right;
            case '>': return left > right;
            case '<': return left < right;
            case '>=': return left >= right;
            case '<=': return left <= right;
            case '==': return left === right;
            case '!=': return left !== right;
            default:
                throw new Error(`Unsupported constraint operator '${node.op}'`);
        }
    }
    throw new Error('Invalid type constraint metadata');
}

function constraintSatisfied(param, value) {
    if (!param.constraint) {
        return true;
    }
    return !!evalConstraintNode(param.constraint, value, param.constraint_var || 'x');
}

function formatParamUsage(params) {
    if (params.length === 0) {
        return '';
    }
    return ' ' + params.map((p) => `[${p.name}: ${p.type}]`).join(' ');
}

function validateMainArgs(rawArgs, metadata) {
    if (!metadata || !metadata.main) {
        return rawArgs.map(Number);
    }

    const params = metadata.main.params || [];
    if (rawArgs.length !== params.length) {
        const expected = params.length === 0
            ? 'no arguments'
            : `${params.length} argument(s): ${params.map((p) => `${p.name}: ${p.type}`).join(', ')}`;
        const got = rawArgs.length === 0 ? 'no arguments' : `${rawArgs.length} argument(s)`;
        throw new Error(
            `main expects ${expected}, but received ${got}.` +
            `\nUsage: lattice <source.lattice>${formatParamUsage(params)}`
        );
    }

    const parsed = [];
    for (let i = 0; i < params.length; i++) {
        const param = params[i];
        const raw = rawArgs[i];
        const value = Number(raw);
        if (raw === '' || !Number.isFinite(value) || !Number.isInteger(value)) {
            throw new Error(
                `Argument '${param.name}' must be of type ${param.type}, but got '${raw}'.`
            );
        }
        if (!constraintSatisfied(param, value)) {
            throw new Error(
                `Argument '${param.name}' must satisfy type ${param.type}, but got ${value}.`
            );
        }
        parsed.push(value);
    }
    return parsed;
}

(async () => {
    let fs, cp;
    if (typeof require !== 'undefined') {
        fs = require('fs');
        cp = require('child_process');
    } else {
        fs = await import('fs');
        cp = await import('child_process');
    }

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
    const mainMetadata = loadMainMetadata(fs, wasmPath);

    let exports;
    let heapPtr = 50000; // Safe start of temp heap for runtime allocations
    function allocate(size) {
        const addr = heapPtr;
        heapPtr += size;
        return addr;
    }

    // Define environment imports matching the ones declared in the emitter
    const imports = {
        env: {
            print_int: function(val) {
                console.log(val);
            },
            print_char: function(val) {
                process.stdout.write(String.fromCodePoint(val));
            },
            print_raw_string: function(addr, len) {
                const view = new DataView(exports.memory.buffer);
                let s = "";
                for (let i = 0; i < len; i++) {
                    s += String.fromCodePoint(view.getInt32(addr + i * 4, true));
                }
                process.stdout.write(s);
            },
            read_int_raw: function(out_ptr) {
                const buffer = Buffer.alloc(1024);
                let bytesRead = 0;
                try {
                    bytesRead = fs.readSync(0, buffer, 0, 1024, null);
                } catch (e) {}
                const mem = new Uint8Array(exports.memory.buffer);
                const view = new DataView(exports.memory.buffer);
                if (bytesRead === 0) {
                    mem[out_ptr] = 1; // None (tag 1)
                    return;
                }
                const line = buffer.toString('utf8', 0, bytesRead).trim();
                const val = parseInt(line, 10);
                if (isNaN(val)) {
                    mem[out_ptr] = 1; // None (tag 1)
                } else {
                    mem[out_ptr] = 0; // Some (tag 0)
                    view.setInt32(out_ptr + 1, val, true);
                }
            },
            read_string_raw: function(out_ptr, max_len) {
                const buffer = Buffer.alloc(1024);
                let bytesRead = 0;
                try {
                    bytesRead = fs.readSync(0, buffer, 0, 1024, null);
                } catch (e) {}
                const mem = new Uint8Array(exports.memory.buffer);
                const view = new DataView(exports.memory.buffer);
                if (bytesRead === 0) {
                    mem[out_ptr] = 1; // None (tag 1)
                    return;
                }
                const line = buffer.toString('utf8', 0, bytesRead).replace(/\r?\n$/, "");
                const finalLen = Math.min(line.length, max_len);
                
                // Write Union tag (Some = 0)
                mem[out_ptr] = 0;
                
                // Allocate String struct, List struct, and characters
                const string_ptr = allocate(8); // len (4 bytes) + data (4 bytes)
                const list_ptr = allocate(4);   // data (4 bytes)
                const char_data_ptr = allocate(max_len * 4);
                
                // Write String pointer to Some variant Value field
                view.setInt32(out_ptr + 1, string_ptr, true);
                // Write len to String len field
                view.setInt32(string_ptr, finalLen, true);
                // Write List pointer to String data field
                view.setInt32(string_ptr + 4, list_ptr, true);
                // Write Group pointer to List data field
                view.setInt32(list_ptr, char_data_ptr, true);
                
                // Write character code points
                for (let i = 0; i < finalLen; i++) {
                    view.setInt32(char_data_ptr + i * 4, line.codePointAt(i), true);
                }
            },
            read_file_raw: function(path_ptr, path_len, out_ptr, max_len) {
                const view = new DataView(exports.memory.buffer);
                const list_ptr = view.getInt32(path_ptr + 4, true);
                const char_data_ptr = view.getInt32(list_ptr, true);
                let path = "";
                for (let i = 0; i < path_len; i++) {
                    path += String.fromCodePoint(view.getInt32(char_data_ptr + i * 4, true));
                }
                const mem = new Uint8Array(exports.memory.buffer);
                try {
                    if (!fs.existsSync(path)) {
                        mem[out_ptr] = 1; // None (tag 1)
                        return;
                    }
                    const content = fs.readFileSync(path, 'utf8');
                    const finalLen = Math.min(content.length, max_len);
                    
                    // Write Union tag (Some = 0)
                    mem[out_ptr] = 0;
                    
                    // Allocate String struct, List struct, and characters
                    const out_string_ptr = allocate(8);
                    const out_list_ptr = allocate(4);
                    const out_char_data_ptr = allocate(max_len * 4);
                    
                    // Write String pointer to Some variant Value field
                    view.setInt32(out_ptr + 1, out_string_ptr, true);
                    // Write len to String len field
                    view.setInt32(out_string_ptr, finalLen, true);
                    // Write List pointer to String data field
                    view.setInt32(out_string_ptr + 4, out_list_ptr, true);
                    // Write Group pointer to List data field
                    view.setInt32(out_list_ptr, out_char_data_ptr, true);
                    
                    // Write character code points
                    for (let i = 0; i < finalLen; i++) {
                        view.setInt32(out_char_data_ptr + i * 4, content.codePointAt(i), true);
                    }
                } catch (e) {
                    mem[out_ptr] = 1; // None (tag 1)
                }
            },
            http_get_raw: function(url_ptr, url_len, out_ptr, max_len) {
                const view = new DataView(exports.memory.buffer);
                const mem = new Uint8Array(exports.memory.buffer);
                const memorySize = mem.length;
                if (url_ptr < 0 || url_ptr + 8 > memorySize || url_len < 0 || url_len > 4096) {
                    mem[out_ptr] = 1;
                    return;
                }
                const list_ptr = view.getInt32(url_ptr + 4, true);
                if (list_ptr < 0 || list_ptr + 4 > memorySize) {
                    mem[out_ptr] = 1;
                    return;
                }
                const char_data_ptr = view.getInt32(list_ptr, true);
                if (char_data_ptr < 0 || char_data_ptr + url_len * 4 > memorySize) {
                    mem[out_ptr] = 1;
                    return;
                }
                let url = "";
                for (let i = 0; i < url_len; i++) {
                    url += String.fromCodePoint(view.getInt32(char_data_ptr + i * 4, true));
                }
                try {
                    const content = cp.execFileSync('curl', ['-s', '--max-time', '5', url], { encoding: 'utf8' });
                    const finalLen = Math.min(content.length, max_len);
                    
                    // Write Union tag (Some = 0)
                    mem[out_ptr] = 0;
                    
                    // Allocate String struct, List struct, and characters
                    const out_string_ptr = allocate(8);
                    const out_list_ptr = allocate(4);
                    const out_char_data_ptr = allocate(max_len * 4);
                    
                    // Write String pointer to Some variant Value field
                    view.setInt32(out_ptr + 1, out_string_ptr, true);
                    // Write len to String len field
                    view.setInt32(out_string_ptr, finalLen, true);
                    // Write List pointer to String data field
                    view.setInt32(out_string_ptr + 4, out_list_ptr, true);
                    // Write Group pointer to List data field
                    view.setInt32(out_list_ptr, out_char_data_ptr, true);
                    
                    // Write character code points
                    for (let i = 0; i < finalLen; i++) {
                        view.setInt32(out_char_data_ptr + i * 4, content.codePointAt(i), true);
                    }
                } catch (e) {
                    mem[out_ptr] = 1; // None (tag 1)
                }
            }
        }
    };

    try {
        const result = await WebAssembly.instantiate(wasmBuffer, imports);
        exports = result.instance.exports;
    } catch (err) {
        console.error("WebAssembly Instantiation Failed:", err);
        process.exit(1);
    }

    try {
        if (exports.main) {
            const rawArgs = process.argv.slice(3);
            const args = validateMainArgs(rawArgs, mainMetadata);
            const ret = exports.main(...args);
            if (ret !== undefined) {
                console.log(ret);
            }
        } else if (exports.app_entry) {
            console.log("Running app_entry()...");
            const start = Date.now();
            exports.app_entry();
            const elapsed = Date.now() - start;
            console.log(`Execution finished (completed in ${elapsed}ms)`);
        } else {
            console.log("WASM loaded, but found no entry point 'main' or 'app_entry'.");
        }
    } catch (err) {
        console.error("Error:", err.message || err);
        process.exit(1);
    }
})();
