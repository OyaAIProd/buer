/**
 * BUER TS/TSX dataflow analyzer — §4.2a Unit B2
 *
 * Input  (stdin):  JSON { project_root, file_path, define_name }
 * Output (stdout): JSON { edges: [{producer_file, producer_define}], errors: [] }
 *
 * For the CONSUMER define (define_name in file_path), finds all call expressions
 * in its body where the return value is consumed and the callee resolves to a
 * project-internal define.  Returns one edge per consumed callee.
 *
 * Edge direction: caller (consumer) depends on callee (producer).
 *   gd.py builds: from_det = producer's current version, to_det = consumer's det.
 *
 * Gain over call-graph archive:
 *   fire-and-forget calls eliminated — parent node is ExpressionStatement (or
 *   ExpressionStatement wrapping AwaitExpression) → no edge → no false positives.
 *   Type-driven alias resolution via ts.createProgram + checker.
 *
 * Residual limits (§3.7 honest annotation):
 *   Dynamic dispatch, string indexing, Proxy, computed property names — still
 *   unresolvable (same class as call-graph). Declared at output for transparency.
 *
 * Incremental: only the single consumer define is analyzed; no full-project recompute.
 * Performance note: ts.createProgram reads the whole project graph once per call.
 *   Subprocess startup + program creation is O(project size); expect 1-10 s
 *   on realistic TS projects. Timeout protection is enforced by the Python caller.
 */

import { readFileSync } from 'fs';
import path from 'path';
import { createRequire } from 'module';

function main() {
  let input;
  try {
    input = JSON.parse(readFileSync(0, 'utf-8'));
  } catch (e) {
    emit({ edges: [], errors: [`invalid JSON input: ${e.message}`] });
    return;
  }
  const { project_root, file_path, define_name } = input;

  // Load typescript from the project's node_modules, not from wherever this
  // script lives.  createRequire with a project-root base resolves 'typescript'
  // through standard Node.js module resolution starting at project_root.
  const require = createRequire(project_root + path.sep);
  let ts;
  try {
    ts = require('typescript');
  } catch (e) {
    emit({ edges: [], errors: [`typescript not importable from ${project_root}/node_modules: ${e.message}`] });
    return;
  }

  // Parse tsconfig from project root
  const tsconfigPath = path.join(project_root, 'tsconfig.json');
  const configFile = ts.readConfigFile(tsconfigPath, ts.sys.readFile);
  if (configFile.error) {
    emit({ edges: [], errors: [`tsconfig read error: ${diagText(configFile.error)}`] });
    return;
  }
  const parsed = ts.parseJsonConfigFileContent(configFile.config, ts.sys, project_root);
  if (parsed.errors.length > 0) {
    emit({ edges: [], errors: parsed.errors.map(diagText) });
    return;
  }

  // Create program (all files in tsconfig; incremental since we only walk one define)
  const program = ts.createProgram(parsed.fileNames, parsed.options);
  const checker = program.getTypeChecker();

  // Resolve consumer file to absolute path
  const absFilePath = path.isAbsolute(file_path)
    ? file_path
    : path.resolve(project_root, file_path);

  const sourceFile = program.getSourceFile(absFilePath);
  if (!sourceFile) {
    // File not in program (e.g. excluded by tsconfig) — return empty, not an error.
    emit({ edges: [], errors: [] });
    return;
  }

  // Find the consumer define's function body
  const body = findFunctionBody(ts, sourceFile, define_name);
  if (!body) {
    // Define not found (dynamic/anonymous/not yet committed) — empty, not an error.
    emit({ edges: [], errors: [] });
    return;
  }

  // Collect consumed-callee edges (cross_define_dataflow)
  const edges = [];
  const edgeSeen = new Set();
  collectConsumedCallees(ts, checker, body, project_root, edges, edgeSeen);

  // Collect unresolved calls (§2.8 dangling reference)
  const unresolvedCalls = [];
  const unresolvedSeen = new Set();
  collectUnresolvedCallsInBody(ts, checker, body, define_name, absFilePath, unresolvedCalls, unresolvedSeen);

  emit({ edges, errors: [], unresolved_calls: unresolvedCalls });
}

function emit(data) {
  process.stdout.write(JSON.stringify(data) + '\n');
}

function diagText(d) {
  return typeof d.messageText === 'string' ? d.messageText : String(d.messageText.messageText ?? d.messageText);
}

// ── consumer define body lookup ────────────────────────────────────────────────

function findFunctionBody(ts, sourceFile, defineName) {
  const dot = defineName.lastIndexOf('.');
  if (dot === -1) {
    return findTopLevelBody(ts, sourceFile, defineName);
  }
  const className  = defineName.slice(0, dot);
  const methodName = defineName.slice(dot + 1);
  return findMethodBody(ts, sourceFile, className, methodName);
}

function findTopLevelBody(ts, sourceFile, name) {
  for (const stmt of sourceFile.statements) {
    // function foo() {}  or  export function foo() {}
    if (ts.isFunctionDeclaration(stmt) && stmt.name?.text === name && stmt.body) {
      return stmt.body;
    }
    // const/let/var foo = () => {} or foo = function() {}
    if (ts.isVariableStatement(stmt)) {
      for (const decl of stmt.declarationList.declarations) {
        if (ts.isIdentifier(decl.name) && decl.name.text === name && decl.initializer) {
          const init = decl.initializer;
          if (ts.isArrowFunction(init) || ts.isFunctionExpression(init)) {
            // Arrow body may be an expression (not a block); return it too.
            return init.body ?? null;
          }
        }
      }
    }
  }
  return null;
}

function findMethodBody(ts, sourceFile, className, methodName) {
  for (const stmt of sourceFile.statements) {
    if (!ts.isClassDeclaration(stmt) || stmt.name?.text !== className) continue;
    for (const member of stmt.members) {
      if (
        ts.isMethodDeclaration(member) &&
        ts.isIdentifier(member.name) &&
        member.name.text === methodName &&
        member.body
      ) {
        return member.body;
      }
    }
  }
  return null;
}

// ── consumed-callee collection ─────────────────────────────────────────────────

function collectConsumedCallees(ts, checker, bodyNode, projectRoot, edges, seen) {
  function visit(node) {
    if (ts.isCallExpression(node)) {
      if (isConsumed(ts, node)) {
        const edge = resolveCallee(ts, checker, node, projectRoot);
        if (edge) {
          const key = `${edge.producer_file}|${edge.producer_define}`;
          if (!seen.has(key)) {
            seen.add(key);
            edges.push(edge);
          }
        }
      }
    }
    // Boundary: do not descend into nested function/class definitions
    if (
      ts.isFunctionDeclaration(node) ||
      ts.isFunctionExpression(node) ||
      ts.isArrowFunction(node) ||
      ts.isMethodDeclaration(node) ||
      ts.isClassDeclaration(node) ||
      ts.isClassExpression(node)
    ) {
      return;
    }
    ts.forEachChild(node, visit);
  }
  ts.forEachChild(bodyNode, visit);
}

function isConsumed(ts, callExpr) {
  const parent = callExpr.parent;
  // Direct fire-and-forget: foo();
  if (ts.isExpressionStatement(parent)) return false;
  // Async fire-and-forget: await foo();  (AwaitExpression → ExpressionStatement)
  if (ts.isAwaitExpression(parent) && ts.isExpressionStatement(parent.parent)) return false;
  // Everything else: return value is used in an expression, assignment, condition, etc.
  return true;
}

// ── unresolved-call collection (§2.8 dangling reference) ───────────────────────
//
// Traverses ALL call expressions in the define body including nested functions
// (different boundary rule from consumed-callee collection whose purpose is
// different).  A call is "unresolved" when the TypeScript type checker cannot
// find a resolved symbol for the callee.
// Calls that resolve to node_modules / stdlib are NOT flagged: TS always has
// symbols for those via lib.d.ts and @types packages.
//
// Honest boundary (§2.8): bare identifier detection (foo()) is reliable — TS
// returns undefined from getSymbolAtLocation for undeclared names. Property-access
// detection (obj.typo()) depends on TS type completeness for the receiver; if the
// receiver itself is unresolved (intrinsicName === 'error'), we skip to avoid
// double-reporting. getTypeAtLocation can throw for certain node types (TS issues
// #48878 / #62190) — those cases are skipped conservatively (宁漏不误报).

function getCalleeLeafName(ts, callee) {
  if (ts.isIdentifier(callee)) return callee.text;
  // property access: this.foo(), obj.bar() → return the method name only
  if (ts.isPropertyAccessExpression(callee) && ts.isIdentifier(callee.name)) {
    return callee.name.text;
  }
  return null;
}

// Returns true if `callee` node refers to an unresolved symbol.
function checkCalleeUnresolved(ts, checker, callee) {
  if (ts.isIdentifier(callee)) {
    let sym;
    try { sym = checker.getSymbolAtLocation(callee); } catch { return false; }
    return !sym;
  }
  if (ts.isPropertyAccessExpression(callee) && ts.isIdentifier(callee.name)) {
    // Guard: if receiver itself is unresolved skip — avoids double-reporting when
    // the whole call chain is broken and only the root identifier matters.
    const receiverExpr = callee.expression;
    try {
      const receiverType = checker.getTypeAtLocation(receiverExpr);
      // intrinsicName === 'error' is TS internal error sentinel (stable, not in public typings)
      if (receiverType?.intrinsicName === 'error') return false;
    } catch {
      return false;  // conservative: can't determine receiver type
    }
    let sym;
    try { sym = checker.getSymbolAtLocation(callee); } catch { return false; }
    if (!sym) return true;
    // Some TS versions return a synthetic symbol with no declarations for missing properties
    const decls = sym.getDeclarations?.();
    return !decls || decls.length === 0;
  }
  return false;
}

function collectUnresolvedCallsInBody(ts, checker, bodyNode, defineName, absFilePath, unresolvedCalls, seen) {
  function visit(node) {
    if (ts.isCallExpression(node)) {
      const leafName = getCalleeLeafName(ts, node.expression);
      if (leafName && checkCalleeUnresolved(ts, checker, node.expression)) {
        const key = `${defineName}|${leafName}`;
        if (!seen.has(key)) {
          seen.add(key);
          const sf = node.getSourceFile();
          const { line } = sf.getLineAndCharacterOfPosition(node.getStart());
          unresolvedCalls.push({
            caller_define: defineName,
            callee_text: leafName,
            file: absFilePath,
            line: line + 1,
          });
        }
      }
    }
    ts.forEachChild(node, visit);  // full traversal including nested functions
  }
  visit(bodyNode);  // check body itself first (handles arrow expression bodies)
}

// ── value alias resolution ─────────────────────────────────────────────────────
//
// Level 1: const fn = target; fn(x) → resolve to target's declaration.
//   Only follows const (not let/var) + plain Identifier initializers.
//   Conditional/ternary/call initializers are left unresolved (no false edges).
//
// Level 2: readonly property aliases — two sub-cases:
//   (a) as-const literal: symbol resolves to value-layer PropertyAssignment directly.
//   (b) type-annotation readonly (const x: {readonly p: T} = {p: fn}): symbol resolves
//       to type-layer PropertySignature; value layer is found via findValuePropertyAssignment.
//   Non-readonly properties are left unresolved.
//
// Depth limit: 8 hops. Cycle detection via `seen` Set of declaration positions.

function followValueAlias(checker, ts, decl, depth, seen) {
  if (depth > 8) return decl;

  let init = null;

  if (ts.isVariableDeclaration(decl)) {
    const parent = decl.parent;
    if (!parent || !ts.isVariableDeclarationList(parent)) return decl;
    // NodeFlags.Const — only follow const, not let/var
    if (!(parent.flags & ts.NodeFlags.Const)) return decl;
    init = decl.initializer ?? null;
  } else if (ts.isPropertyAssignment(decl)) {
    // Readonly guard is enforced by the caller (resolveCallee) before this is invoked
    init = decl.initializer ?? null;
  } else {
    return decl;
  }

  // Only follow plain Identifier initializers — not conditionals, ternaries, calls, etc.
  if (!init || !ts.isIdentifier(init)) return decl;

  let sym;
  try { sym = checker.getSymbolAtLocation(init); } catch { return decl; }
  if (!sym) return decl;

  if (sym.flags & ts.SymbolFlags.Alias) {
    try { sym = checker.getAliasedSymbol(sym); } catch { return decl; }
  }

  const targetDecls = sym.getDeclarations?.();
  if (!targetDecls || targetDecls.length === 0) return decl;
  const targetDecl = targetDecls[0];

  if (seen.has(targetDecl.pos)) return decl;
  seen.add(targetDecl.pos);

  return followValueAlias(checker, ts, targetDecl, depth + 1, seen);
}

function isReadonlyProperty(checker, ts, propAccessNode) {
  if (!ts.isPropertyAccessExpression(propAccessNode)) return false;
  const receiverExpr = propAccessNode.expression;
  const propName = propAccessNode.name?.text;
  if (!propName) return false;

  // Method 1: checker.isReadonlySymbol (not universally available — try first)
  let receiverType;
  try { receiverType = checker.getTypeAtLocation(receiverExpr); } catch { return false; }
  if (!receiverType) return false;
  const propSymbol = receiverType.getProperty(propName);
  if (propSymbol && typeof checker.isReadonlySymbol === 'function') {
    try { if (checker.isReadonlySymbol(propSymbol)) return true; } catch { /* fallback */ }
  }

  // Method 2: detect `as const` assertion on receiver's variable declaration.
  // `x as const` is AsExpression { type: TypeReferenceNode { typeName: "const" } }
  let receiverSym;
  try { receiverSym = checker.getSymbolAtLocation(receiverExpr); } catch { return false; }
  for (const d of (receiverSym?.getDeclarations?.() ?? [])) {
    if (ts.isVariableDeclaration(d) && d.initializer) {
      const init = d.initializer;
      if (
        ts.isAsExpression(init) &&
        ts.isTypeReferenceNode(init.type) &&
        ts.isIdentifier(init.type.typeName) &&
        init.type.typeName.text === 'const'
      ) {
        return true;
      }
    }
  }

  // Method 3: explicit readonly keyword on PropertySignature / PropertyDeclaration
  for (const d of (propSymbol?.getDeclarations?.() ?? [])) {
    if (ts.isPropertySignature(d) || ts.isPropertyDeclaration(d)) {
      const mods = [...((ts.getModifiers ? ts.getModifiers(d) : d.modifiers) ?? [])];
      if (mods.some(m => m.kind === ts.SyntaxKind.ReadonlyKeyword)) return true;
    }
  }
  return false;
}

function findValuePropertyAssignment(checker, ts, propAccessNode) {
  // For type-annotation readonly: the callee symbol resolves to a type-layer
  // PropertySignature rather than a value-layer PropertyAssignment.  Locate the
  // matching PropertyAssignment in the receiver variable's object literal initializer.
  const propName = propAccessNode.name?.text;
  if (!propName) return null;
  let recvSym;
  try { recvSym = checker.getSymbolAtLocation(propAccessNode.expression); } catch { return null; }
  if (!recvSym) return null;
  if (recvSym.flags & ts.SymbolFlags.Alias) {
    try { recvSym = checker.getAliasedSymbol(recvSym); } catch { return null; }
  }
  for (const d of (recvSym.getDeclarations?.() ?? [])) {
    if (!ts.isVariableDeclaration(d) || !d.initializer) continue;
    // Unwrap `as const` AsExpression if present
    const lit = ts.isAsExpression(d.initializer) ? d.initializer.expression : d.initializer;
    if (!ts.isObjectLiteralExpression(lit)) continue;
    for (const prop of lit.properties) {
      if (
        ts.isPropertyAssignment(prop) &&
        ts.isIdentifier(prop.name) &&
        prop.name.text === propName
      ) {
        return prop;
      }
    }
  }
  return null;
}

// ── callee → project define resolution ────────────────────────────────────────

function resolveCallee(ts, checker, callExpr, projectRoot) {
  const callee = callExpr.expression;
  let symbol;
  try {
    symbol = checker.getSymbolAtLocation(callee);
  } catch {
    return null;
  }
  if (!symbol) return null;

  // Resolve import aliases (import { decode } from './auth' — decode is an alias)
  if (symbol.flags & ts.SymbolFlags.Alias) {
    try {
      symbol = checker.getAliasedSymbol(symbol);
    } catch {
      return null;
    }
  }

  const decls = symbol.getDeclarations?.();
  if (!decls || decls.length === 0) return null;
  let decl = decls[0];

  // Value alias resolution: follow const Identifier initializers (Level 1)
  // and readonly property Identifier initializers (Level 2).
  // `decl` is updated to the resolved declaration; fileName is derived below.
  if (ts.isVariableDeclaration(decl)) {
    const seen = new Set([decl.pos]);
    decl = followValueAlias(checker, ts, decl, 0, seen);
  } else if (
    ts.isPropertyAccessExpression(callee) &&
    isReadonlyProperty(checker, ts, callee)
  ) {
    if (ts.isPropertyAssignment(decl)) {
      // as-const: symbol already points to value-layer PropertyAssignment
      const seen = new Set([decl.pos]);
      decl = followValueAlias(checker, ts, decl, 0, seen);
    } else if (ts.isPropertySignature(decl)) {
      // Type-annotation readonly: symbol points to type-layer PropertySignature;
      // locate the value-layer PropertyAssignment from receiver's object literal.
      const valuePA = findValuePropertyAssignment(checker, ts, callee);
      if (valuePA) {
        const seen = new Set([valuePA.pos]);
        decl = followValueAlias(checker, ts, valuePA, 0, seen);
      }
    }
  }

  // Compute fileName from resolved decl — may differ from original if alias crosses files
  const srcFile = decl.getSourceFile();
  const fileName = srcFile.fileName;

  // Skip stdlib and node_modules
  if (!fileName.startsWith(projectRoot) || fileName.includes('node_modules')) return null;

  let defineName = null;

  if (ts.isFunctionDeclaration(decl) || ts.isFunctionExpression(decl)) {
    defineName = decl.name?.text ?? null;
  } else if (ts.isArrowFunction(decl)) {
    const vd = decl.parent;
    if (vd && ts.isVariableDeclaration(vd) && ts.isIdentifier(vd.name)) {
      defineName = vd.name.text;
    }
  } else if (ts.isMethodDeclaration(decl)) {
    if (!ts.isIdentifier(decl.name)) return null;
    const mname = decl.name.text;
    let cls = decl.parent;
    while (cls && !ts.isClassDeclaration(cls) && !ts.isClassExpression(cls)) {
      cls = cls.parent;
    }
    const cname = cls?.name?.text;
    defineName = cname ? `${cname}.${mname}` : mname;
  } else if (ts.isVariableDeclaration(decl)) {
    if (ts.isIdentifier(decl.name)) defineName = decl.name.text;
  }

  if (!defineName) return null;
  return { producer_file: fileName, producer_define: defineName };
}

main();
