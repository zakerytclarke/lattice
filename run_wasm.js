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

const USER_RECORD_SIZE = 36;

function jsonFieldSlice(jsonText, key, value) {
    const val = String(value);
    const patterns = [`"${key}":"${val}"`, `"${key}": "${val}"`];
    for (const pattern of patterns) {
        const idx = jsonText.indexOf(pattern);
        if (idx >= 0) {
            const start = idx + pattern.indexOf(val);
            return { start, len: val.length };
        }
    }
    return { start: 0, len: 0 };
}

function writeUserRecord(view, base, jsonText, record) {
    const name = jsonFieldSlice(jsonText, 'name', record.name || '');
    const email = jsonFieldSlice(jsonText, 'email', record.email || '');
    const phone = jsonFieldSlice(jsonText, 'phone', record.phone || '');
    const birthdate = jsonFieldSlice(jsonText, 'birthdate', record.birthdate || '');
    view.setInt32(base + 0, name.start, true);
    view.setInt32(base + 4, name.len, true);
    view.setInt32(base + 8, email.start, true);
    view.setInt32(base + 12, email.len, true);
    view.setInt32(base + 16, phone.start, true);
    view.setInt32(base + 20, phone.len, true);
    view.setInt32(base + 24, (record.age | 0), true);
    view.setInt32(base + 28, birthdate.start, true);
    view.setInt32(base + 32, birthdate.len, true);
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

function schemaTypeSize(schema) {
    switch (schema.base) {
        case 'Integer':
        case 'Bool':
            return 4;
        case 'Rational':
            return 8;
        case 'String':
            return 8 + schema.max_len * 4;
        case 'List':
            return schema.length * schemaTypeSize(schema.elem);
        default:
            break;
    }
    if (schema.fields) {
        let size = 0;
        for (const field of schema.fields) {
            size += schemaTypeSize(field);
        }
        return size;
    }
    if (schema.base === 'Rational') {
        return 8;
    }
    if (schema.base === 'String') {
        return (schema.max_len || 64) * 4 + 4;
    }
    if (schema.base === 'StrSlice') {
        return 8;
    }
    return 4;
}

function writeStructFieldFromJson(view, base, offset, fieldSchema, jsonText, record, fieldName) {
    switch (fieldSchema.base) {
        case 'Integer':
            view.setInt32(base + offset, (record[fieldName] | 0) || 0, true);
            return 4;
        case 'Bool':
            view.setInt32(base + offset, record[fieldName] ? 1 : 0, true);
            return 4;
        case 'Rational': {
            const value = Number(record[fieldName]);
            const scaled = Math.round(value * 100);
            view.setInt32(base + offset, scaled, true);
            view.setInt32(base + offset + 4, 100, true);
            return 8;
        }
        case 'String': {
            const maxLen = fieldSchema.max_len || 64;
            const text = String(record[fieldName] ?? '');
            const finalLen = Math.min(text.length, maxLen);
            view.setInt32(base + offset, finalLen, true);
            const listPtr = allocate(4);
            view.setInt32(base + offset + 4, listPtr, true);
            const charDataPtr = allocate(maxLen * 4);
            view.setInt32(listPtr, charDataPtr, true);
            for (let i = 0; i < finalLen; i++) {
                view.setInt32(charDataPtr + i * 4, text.codePointAt(i), true);
            }
            return maxLen * 4 + 4;
        }
        case 'StrSlice': {
            const slice = jsonFieldSlice(jsonText, fieldName, record[fieldName] || '');
            view.setInt32(base + offset, slice.start, true);
            view.setInt32(base + offset + 4, slice.len, true);
            return 8;
        }
        default:
            if (fieldSchema.fields) {
                const subRecord = record && record[fieldName];
                if (!subRecord || typeof subRecord !== 'object') {
                    return schemaTypeSize(fieldSchema);
                }
                let subOffset = 0;
                for (const subField of fieldSchema.fields) {
                    subOffset += writeStructFieldFromJson(
                        view,
                        base + offset,
                        subOffset,
                        subField,
                        jsonText,
                        subRecord,
                        subField.name
                    );
                }
                return subOffset;
            }
            throw new Error(`unsupported read_file struct field type ${fieldSchema.base}`);
    }
}

function resolveJsonRecord(data, innerSchema) {
    if (innerSchema.fields && data && typeof data === 'object' && !Array.isArray(data)) {
        const hasCurrentField = innerSchema.fields.some((field) => field.name === 'current');
        if (
            !hasCurrentField
            && data.current
            && typeof data.current === 'object'
            && !Array.isArray(data.current)
        ) {
            return data.current;
        }
    }
    return data;
}

function writeTypedJsonInput(view, allocate, inner, content, outPtr) {
    const mem = new Uint8Array(view.buffer);
    if (inner.base === 'String') {
        const maxLen = inner.max_len;
        if (content.length > maxLen) {
            mem[outPtr] = 1;
            return;
        }
        writeReadFileInputString(view, allocate, { inner }, content, outPtr);
        return;
    }
    if (inner.base === 'List') {
        let records;
        try {
            records = JSON.parse(content);
        } catch (e) {
            mem[outPtr] = 1;
            return;
        }
        if (!Array.isArray(records) || records.length > inner.length) {
            mem[outPtr] = 1;
            return;
        }
        writeReadFileInputList(view, allocate, inner, content, records, outPtr);
        return;
    }
    if (inner.fields) {
        let data;
        try {
            data = JSON.parse(content);
        } catch (e) {
            mem[outPtr] = 1;
            return;
        }
        const record = resolveJsonRecord(data, inner);
        if (!record || typeof record !== 'object' || Array.isArray(record)) {
            mem[outPtr] = 1;
            return;
        }
        mem[outPtr] = 0;
        writeStructFromJsonRecord(view, outPtr + 1, inner, content, record);
        return;
    }
    mem[outPtr] = 1;
}

function writeStructFromJsonRecord(view, base, schema, jsonText, record) {
    let offset = 0;
    for (const field of schema.fields || []) {
        offset += writeStructFieldFromJson(
            view,
            base,
            offset,
            field,
            jsonText,
            record,
            field.name
        );
    }
}

function writeInputSome(view, allocate, outPtr, innerSchema, parsed) {
    const mem = new Uint8Array(view.buffer);
    mem[outPtr] = 0;
    switch (parsed.kind) {
        case 'Integer':
        case 'Bool':
            view.setInt32(outPtr + 1, parsed.value, true);
            return;
        case 'Rational': {
            const payloadPtr = allocate(8);
            view.setInt32(outPtr + 1, payloadPtr, true);
            view.setInt32(payloadPtr, parsed.num, true);
            view.setInt32(payloadPtr + 4, parsed.den, true);
            return;
        }
        case 'String': {
            const stringPtr = allocate(8);
            view.setInt32(outPtr + 1, stringPtr, true);
            writeStringValue(view, allocate, stringPtr, parsed.value, parsed.maxLen);
            return;
        }
        case 'List': {
            const elemSize = schemaTypeSize(innerSchema.elem);
            const listDataPtr = allocate(innerSchema.length * elemSize);
            for (let i = 0; i < parsed.length; i++) {
                writeInputValue(view, allocate, listDataPtr + i * elemSize, parsed.elems[i]);
            }
            const listWrapperPtr = allocate(4);
            view.setInt32(listWrapperPtr, listDataPtr, true);
            view.setInt32(outPtr + 1, listWrapperPtr, true);
            return;
        }
        default:
            throw new Error(`unsupported parsed input kind ${parsed.kind}`);
    }
}

function writeReadFileInputList(view, allocate, innerSchema, content, records, outPtr) {
    const mem = new Uint8Array(view.buffer);
    const elemSize = schemaTypeSize(innerSchema.elem);
    const listDataPtr = allocate(innerSchema.length * elemSize);
    writeReadFileList(view, allocate, innerSchema, content, records, listDataPtr);
    const listWrapperPtr = allocate(4);
    view.setInt32(listWrapperPtr, listDataPtr, true);
    mem[outPtr] = 0;
    view.setInt32(outPtr + 1, listWrapperPtr, true);
}

function writeReadFileList(view, allocate, schema, jsonText, records, listDataPtr) {
    const elemSchema = schema.elem;
    const elemSize = schemaTypeSize(elemSchema);
    const count = Math.min(records.length, schema.length);
    for (let i = 0; i < count; i++) {
        if (elemSchema.base === 'Integer') {
            view.setInt32(listDataPtr + i * elemSize, (records[i] | 0) || 0, true);
        } else if (elemSchema.base === 'Bool') {
            view.setInt32(listDataPtr + i * elemSize, records[i] ? 1 : 0, true);
        } else if (elemSchema.fields) {
            writeStructFromJsonRecord(
                view,
                listDataPtr + i * elemSize,
                elemSchema,
                jsonText,
                records[i]
            );
        } else {
            throw new Error(`unsupported read_file list element type ${elemSchema.base}`);
        }
    }
    return count;
}

function writeReadFileInputString(view, allocate, schema, content, outPtr) {
    const maxLen = schema.inner.max_len;
    const finalLen = Math.min(content.length, maxLen);
    const mem = new Uint8Array(view.buffer);
    mem[outPtr] = 0;
    const out_string_ptr = allocate(8);
    const out_list_ptr = allocate(4);
    const out_char_data_ptr = allocate(maxLen * 4);
    view.setInt32(outPtr + 1, out_string_ptr, true);
    view.setInt32(out_string_ptr, finalLen, true);
    view.setInt32(out_string_ptr + 4, out_list_ptr, true);
    view.setInt32(out_list_ptr, out_char_data_ptr, true);
    for (let i = 0; i < finalLen; i++) {
        view.setInt32(out_char_data_ptr + i * 4, content.codePointAt(i), true);
    }
}

function readPathString(view, path_ptr, path_len) {
    const list_ptr = view.getInt32(path_ptr + 4, true);
    const char_data_ptr = view.getInt32(list_ptr, true);
    let path = "";
    for (let i = 0; i < path_len; i++) {
        path += String.fromCodePoint(view.getInt32(char_data_ptr + i * 4, true));
    }
    return path;
}

// main() parameters are all Input[T]. Each CLI argument is parsed into its inner
// type T: a valid argument becomes Some(value), an invalid one becomes None. The
// count of arguments must still match (a usage error), but a present-but-invalid
// argument is surfaced to the program as None rather than aborting.
function validateMainArgs(rawArgs, metadata) {
    if (!metadata || !metadata.main) {
        return [];
    }

    const params = metadata.main.params || [];
    const inputArgs = metadata.main.input_args || [];

    if (rawArgs.length !== inputArgs.length) {
        const expected = inputArgs.length === 0
            ? 'no arguments'
            : `${inputArgs.length} argument(s): ${params.map((p) => `${p.name}: ${p.type}`).join(', ')}`;
        const got = rawArgs.length === 0 ? 'no arguments' : `${rawArgs.length} argument(s)`;
        throw new Error(
            `main expects ${expected}, but received ${got}.` +
            `\nUsage: lattice <source.lattice>${formatParamUsage(params)}`
        );
    }

    return inputArgs.map((spec, i) => {
        let parsed = null;
        try {
            parsed = parseInputValue(spec, rawArgs[i]);
        } catch (e) {
            parsed = null; // invalid argument -> None
        }
        return { spec, parsed };
    });
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
    let view = null;
    function refreshView() {
        view = new DataView(exports.memory.buffer);
        return view;
    }
    function allocate(size) {
        let mem = new Uint8Array(exports.memory.buffer);
        if (heapPtr + size > mem.length) {
            const pagesNeeded = Math.ceil((heapPtr + size - mem.length) / 65536);
            exports.memory.grow(pagesNeeded);
        }
        const addr = heapPtr;
        heapPtr += size;
        refreshView();
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
            read_file_typed_raw: function(call_id, path_ptr, path_len, out_ptr) {
                refreshView();
                let mem = new Uint8Array(exports.memory.buffer);
                const schema = mainMetadata && mainMetadata.read_file_calls
                    ? mainMetadata.read_file_calls.find((c) => c.id === call_id)
                    : null;
                if (!schema) {
                    throw new Error(`unknown read_file call id ${call_id}`);
                }
                if (schema.base !== 'Input' || !schema.inner) {
                    throw new Error(`read_file at line ${schema.line}: expected Input[T] annotation`);
                }
                const path = readPathString(view, path_ptr, path_len);
                try {
                    if (!fs.existsSync(path)) {
                        mem[out_ptr] = 1;
                        return;
                    }
                    const content = fs.readFileSync(path, 'utf8');
                    writeTypedJsonInput(view, allocate, schema.inner, content, out_ptr);
                } catch (e) {
                    mem[out_ptr] = 1;
                }
            },
            http_get_typed_raw: function(call_id, url_ptr, url_len, out_ptr) {
                refreshView();
                let mem = new Uint8Array(exports.memory.buffer);
                const view = new DataView(exports.memory.buffer);
                const schema = mainMetadata && mainMetadata.http_get_calls
                    ? mainMetadata.http_get_calls.find((c) => c.id === call_id)
                    : null;
                if (!schema) {
                    throw new Error(`unknown http_get call id ${call_id}`);
                }
                if (schema.base !== 'Input' || !schema.inner) {
                    throw new Error(`http_get at line ${schema.line}: expected Input[T] annotation`);
                }
                const url = readPathString(view, url_ptr, url_len);
                try {
                    const content = cp.execFileSync('curl', ['-s', '--max-time', '5', url], { encoding: 'utf8' });
                    writeTypedJsonInput(view, allocate, schema.inner, content, out_ptr);
                } catch (e) {
                    mem[out_ptr] = 1;
                }
            },
            read_file_raw: function(path_ptr, path_len, out_ptr, max_len) {
                const view = new DataView(exports.memory.buffer);
                const path = readPathString(view, path_ptr, path_len);
                const mem = new Uint8Array(exports.memory.buffer);
                try {
                    if (!fs.existsSync(path)) {
                        mem[out_ptr] = 1; // None (tag 1)
                        return;
                    }
                    const content = fs.readFileSync(path, 'utf8');
                    writeReadFileInputString(
                        view,
                        allocate,
                        { inner: { base: 'String', max_len } },
                        content,
                        out_ptr
                    );
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
                const innerSchema = schema.base === 'Input' ? schema.inner : schema;
                const prompt = readStringStruct(view, prompt_ptr);
                for (;;) {
                    process.stdout.write(prompt);
                    let line;
                    try {
                        line = readStdinLine(fs);
                    } catch (e) {
                        const mem = new Uint8Array(exports.memory.buffer);
                        mem[out_ptr] = 1;
                        return;
                    }
                    try {
                        const parsed = parseInputValue(innerSchema, line);
                        writeInputSome(view, allocate, out_ptr, innerSchema, parsed);
                        return;
                    } catch (e) {
                        process.stderr.write(`${e.message || e}\n`);
                    }
                }
            },
            parse_users_from_json_raw: function(json_ptr, list_ptr, max_users) {
                const view = new DataView(exports.memory.buffer);
                const jsonText = readStringStruct(view, json_ptr);
                let records;
                try {
                    records = JSON.parse(jsonText);
                } catch (e) {
                    return 0;
                }
                if (!Array.isArray(records)) {
                    return 0;
                }
                const listDataPtr = list_ptr;
                const count = Math.min(records.length, max_users);
                for (let i = 0; i < count; i++) {
                    writeUserRecord(view, listDataPtr + i * USER_RECORD_SIZE, jsonText, records[i]);
                }
                return count;
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
            join_strings_raw: function(out_ptr, handle_ptr, sep_ptr, max_out) {
                const view = new DataView(exports.memory.buffer);
                // handle: [count][strptr_0]...[strptr_{n-1}]
                const count = view.getInt32(handle_ptr, true);
                const sep = readStringStruct(view, sep_ptr);
                const parts = [];
                for (let i = 0; i < count; i++) {
                    const strPtr = view.getInt32(handle_ptr + 4 + i * 4, true);
                    parts.push(readStringStruct(view, strPtr));
                }
                const result = parts.join(sep);
                if (result.length > max_out) {
                    throw new Error(`join result length ${result.length} exceeds capacity ${max_out}`);
                }
                writeStringValue(refreshView(), allocate, out_ptr, result, max_out);
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
            },
            rational_to_string_raw: function(r_ptr, out_ptr, max_len) {
                refreshView();
                const view = new DataView(exports.memory.buffer);
                const num = view.getInt32(r_ptr, true);
                const den = view.getInt32(r_ptr + 4, true);
                const value = den === 0 ? 0 : num / den;
                const text = String(value);
                writeStringValue(view, allocate, out_ptr, text, max_len);
            },
            integer_to_string_raw: function(val, out_ptr, max_len) {
                refreshView();
                const view = new DataView(exports.memory.buffer);
                writeStringValue(view, allocate, out_ptr, String(val), max_len);
            }
        }
    };

    try {
        const result = await WebAssembly.instantiate(wasmBuffer, imports);
        exports = result.instance.exports;
        refreshView();
    } catch (err) {
        console.error("WebAssembly Instantiation Failed:", err);
        process.exit(1);
    }

    try {
        if (exports.main) {
            const rawArgs = process.argv.slice(3);
            const args = validateMainArgs(rawArgs, mainMetadata);
            for (const { spec, parsed } of args) {
                if (parsed === null) {
                    new Uint8Array(exports.memory.buffer)[spec.addr] = 1; // None
                } else {
                    writeInputSome(refreshView(), allocate, spec.addr, spec, parsed);
                }
            }
            const ret = exports.main();
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
