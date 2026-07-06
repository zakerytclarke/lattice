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

function formatParamUsage(params) {
    if (params.length === 0) {
        return '';
    }
    return ' ' + params.map((p) => `[${p.name}: ${p.type}]`).join(' ');
}

function readStringStruct(view, stringPtr) {
    const len = view.getInt32(stringPtr, true);
    const listPtr = view.getInt32(stringPtr + 4, true);
    const charDataPtr = view.getInt32(listPtr, true);
    let s = "";
    for (let i = 0; i < len; i++) {
        s += String.fromCodePoint(view.getInt32(charDataPtr + i * 4, true));
    }
    return s;
}

let stdinRemainder = "";

function readStdinLine(fs) {
    while (true) {
        const match = stdinRemainder.match(/\r?\n/);
        if (match) {
            const idx = match.index;
            const line = stdinRemainder.slice(0, idx);
            stdinRemainder = stdinRemainder.slice(idx + match[0].length);
            return line;
        }
        const buffer = Buffer.alloc(4096);
        const bytesRead = fs.readSync(0, buffer, 0, 4096, null);
        if (bytesRead === 0) {
            if (stdinRemainder.length > 0) {
                const line = stdinRemainder;
                stdinRemainder = "";
                return line;
            }
            throw new Error("unexpected end of input");
        }
        stdinRemainder += buffer.toString("utf8", 0, bytesRead);
    }
}

function parseInputValue(schema, line) {
    const trimmed = line.trim();
    switch (schema.base) {
        case 'Integer': {
            const val = Number(trimmed);
            if (trimmed === '' || !Number.isFinite(val) || !Number.isInteger(val)) {
                throw new Error(`expected ${schema.type}, but got '${line}'`);
            }
            if (!constraintSatisfied(schema, val)) {
                throw new Error(`value ${val} does not satisfy type ${schema.type}`);
            }
            return { kind: 'Integer', value: val };
        }
        case 'Bool': {
            const lower = trimmed.toLowerCase();
            if (lower === 'true' || lower === '1') {
                return { kind: 'Bool', value: 1 };
            }
            if (lower === 'false' || lower === '0') {
                return { kind: 'Bool', value: 0 };
            }
            throw new Error(`expected Bool, but got '${line}'`);
        }
        case 'String': {
            const maxLen = schema.max_len;
            if (trimmed.length > maxLen) {
                throw new Error(`expected String(${maxLen}), but input length is ${trimmed.length}`);
            }
            return { kind: 'String', value: trimmed, maxLen };
        }
        case 'Rational': {
            const parts = trimmed.split('/');
            if (parts.length !== 2) {
                throw new Error(`expected Rational as 'num/den', but got '${line}'`);
            }
            const num = Number(parts[0].trim());
            const den = Number(parts[1].trim());
            if (!Number.isInteger(num) || !Number.isInteger(den) || den === 0) {
                throw new Error(`expected Rational as 'num/den' with non-zero denominator, but got '${line}'`);
            }
            return { kind: 'Rational', num, den };
        }
        case 'List': {
            let parsed;
            try {
                parsed = JSON.parse(trimmed);
            } catch (e) {
                throw new Error(`expected JSON array for ${schema.type}, but got '${line}'`);
            }
            if (!Array.isArray(parsed)) {
                throw new Error(`expected JSON array for ${schema.type}, but got '${line}'`);
            }
            if (parsed.length !== schema.length) {
                throw new Error(`expected ${schema.type} with ${schema.length} elements, but got ${parsed.length}`);
            }
            const elems = parsed.map((item, idx) => {
                const childSchema = { ...schema.elem, type: schema.elem.type || schema.elem.base };
                return parseInputValue(schema.elem, String(item));
            });
            return { kind: 'List', elems, length: schema.length, elemSchema: schema.elem };
        }
        default:
            throw new Error(`unsupported input type ${schema.type}`);
    }
}

function writeStringValue(view, allocate, outPtr, value, maxLen) {
    const finalLen = Math.min(value.length, maxLen);
    view.setInt32(outPtr, finalLen, true);
    const listPtr = allocate(4);
    view.setInt32(outPtr + 4, listPtr, true);
    const charDataPtr = allocate(maxLen * 4);
    view.setInt32(listPtr, charDataPtr, true);
    for (let i = 0; i < finalLen; i++) {
        view.setInt32(charDataPtr + i * 4, value.codePointAt(i), true);
    }
}

function writeInputValue(view, allocate, outPtr, parsed) {
    switch (parsed.kind) {
        case 'Integer':
            view.setInt32(outPtr, parsed.value, true);
            return;
        case 'Bool':
            view.setInt32(outPtr, parsed.value, true);
            return;
        case 'String':
            writeStringValue(view, allocate, outPtr, parsed.value, parsed.maxLen);
            return;
        case 'Rational':
            view.setInt32(outPtr, parsed.num, true);
            view.setInt32(outPtr + 4, parsed.den, true);
            return;
        case 'List': {
            const elemSize = parsed.elemSchema.base === 'Rational' ? 8 : 4;
            for (let i = 0; i < parsed.length; i++) {
                writeInputValue(view, allocate, outPtr + i * elemSize, parsed.elems[i]);
            }
            return;
        }
        default:
            throw new Error(`unsupported parsed input kind ${parsed.kind}`);
    }
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
                    mem[out_ptr] = 0;
                    const out_string_ptr = allocate(8);
                    const out_list_ptr = allocate(4);
                    const out_char_data_ptr = allocate(max_len * 4);
                    view.setInt32(out_ptr + 1, out_string_ptr, true);
                    view.setInt32(out_string_ptr, finalLen, true);
                    view.setInt32(out_string_ptr + 4, out_list_ptr, true);
                    view.setInt32(out_list_ptr, out_char_data_ptr, true);
                    for (let i = 0; i < finalLen; i++) {
                        view.setInt32(out_char_data_ptr + i * 4, content.codePointAt(i), true);
                    }
                } catch (e) {
                    mem[out_ptr] = 1;
                }
            },
            input_typed: function(call_id, prompt_ptr, out_ptr) {
                const view = new DataView(exports.memory.buffer);
                const schema = mainMetadata && mainMetadata.input_calls
                    ? mainMetadata.input_calls.find((c) => c.id === call_id)
                    : null;
                if (!schema) {
                    throw new Error(`unknown input call id ${call_id}`);
                }
                const prompt = readStringStruct(view, prompt_ptr);
                for (;;) {
                    process.stdout.write(prompt);
                    let line;
                    try {
                        line = readStdinLine(fs);
                    } catch (e) {
                        throw new Error(`input at line ${schema.line}: ${e.message || e}`);
                    }
                    try {
                        const parsed = parseInputValue(schema, line);
                        writeInputValue(view, allocate, out_ptr, parsed);
                        return;
                    } catch (e) {
                        process.stderr.write(`${e.message || e}\n`);
                    }
                }
            },
            concat_strings: function(out_ptr, a_ptr, b_ptr, max_out) {
                const view = new DataView(exports.memory.buffer);
                const a_len = view.getInt32(a_ptr, true);
                const b_len = view.getInt32(b_ptr, true);
                const total = a_len + b_len;
                if (total > max_out) {
                    throw new Error(`string concat length ${total} exceeds capacity ${max_out}`);
                }
                const a_list = view.getInt32(a_ptr + 4, true);
                const a_chars = view.getInt32(a_list, true);
                const b_list = view.getInt32(b_ptr + 4, true);
                const b_chars = view.getInt32(b_list, true);
                const out_list_ptr = allocate(4);
                const out_char_ptr = allocate(max_out * 4);
                view.setInt32(out_ptr, total, true);
                view.setInt32(out_ptr + 4, out_list_ptr, true);
                view.setInt32(out_list_ptr, out_char_ptr, true);
                for (let i = 0; i < a_len; i++) {
                    view.setInt32(out_char_ptr + i * 4, view.getInt32(a_chars + i * 4, true), true);
                }
                for (let i = 0; i < b_len; i++) {
                    view.setInt32(out_char_ptr + (a_len + i) * 4, view.getInt32(b_chars + i * 4, true), true);
                }
            },
            strings_equal: function(a_ptr, b_ptr) {
                const view = new DataView(exports.memory.buffer);
                const a_len = view.getInt32(a_ptr, true);
                const b_len = view.getInt32(b_ptr, true);
                if (a_len !== b_len) {
                    return 0;
                }
                const a_list = view.getInt32(a_ptr + 4, true);
                const a_chars = view.getInt32(a_list, true);
                const b_list = view.getInt32(b_ptr + 4, true);
                const b_chars = view.getInt32(b_list, true);
                for (let i = 0; i < a_len; i++) {
                    if (view.getInt32(a_chars + i * 4, true) !== view.getInt32(b_chars + i * 4, true)) {
                        return 0;
                    }
                }
                return 1;
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
