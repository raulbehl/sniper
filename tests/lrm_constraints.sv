// LRM conformance, clause 18 (IEEE 1800-2023): one class per construct. Every class has check(): the
// LRM meaning of its (hard) constraints written as plain procedural SV, the oracle test_lrm.py holds
// fvgen's constraint checker to; Verilator's solver is compared too and its deviations are reported.
// Section numbers are 1800-2023.
package l18_pkg;
  typedef enum bit [2:0] {E0 = 0, E2 = 2, E5 = 5, E7 = 7} e_t;
  typedef struct packed { bit [1:0] hi; e_t lo; } ps_t;         // 18.4: packed struct member enum unrestricted
endpackage

import l18_pkg::*;

// 18.5 expressions: relational, arithmetic with SV widths, signed/unsigned mixes
class L18_expr;
  rand bit [3:0] a, b;
  rand bit signed [3:0] s;
  constraint c { a + b > 4'd9; s < 2; (a * b) % 5 != 3; a - b < 4'd12; }
  function bit check();
    // the constraint expressions themselves: SV's width rules apply (a * b is 32-bit next to the int 5)
    return a + b > 4'd9 && s < 2 && (a * b) % 5 != 3 && a - b < 4'd12;
  endfunction
endclass

// 18.5.6 implication, 18.5.7 if-else (nested, else-if chains), constraint sets
class L18_impl;
  rand bit [3:0] mode, len;
  rand bit hdr;
  constraint c {
    mode == 0 -> len == 0;
    (mode > 3) -> { len > 2; len < 9; }
    if (hdr) len[0] == 0;
    else if (mode == 2) len inside {[4:6]};
    else { len != 7; len != 8; }
    hdr -> mode != 1;
  }
  function bit check();
    bit ok = 1;
    if (mode == 0 && len != 0) ok = 0;
    if (mode > 3 && !(len > 2 && len < 9)) ok = 0;
    if (hdr) begin if (len[0] != 0) ok = 0; end
    else if (mode == 2) begin if (!(len >= 4 && len <= 6)) ok = 0; end
    else if (len == 7 || len == 8) ok = 0;
    if (hdr && mode == 1) ok = 0;
    return ok;
  endfunction
endclass

// 11.4.13 / 18.5.3 inside: values, ranges, $ bounds, array operand, negation
class L18_inside;
  rand bit [3:0] x, y, z;
  bit [3:0] tbl [3] = '{1, 5, 9};
  constraint c { x inside {[2:4], 9, [12:$]}; !(y inside {[$:3], 7}); z inside {tbl}; }
  function bit check();
    return ((x >= 2 && x <= 4) || x == 9 || x >= 12) && !(y <= 3 || y == 7) &&
           (z == tbl[0] || z == tbl[1] || z == tbl[2]);
  endfunction
endclass

// 18.5.3 dist: the legal values are those with a non-zero weight (:= and :/, values and ranges)
class L18_dist;
  rand bit [3:0] w, v;
  constraint c { w dist {0 := 1, [1:3] :/ 2, 7 := 0, [8:9] := 3}; v dist {[2:5] :/ 1, 15 := 1}; }
  function bit check();
    return (w == 0 || (w >= 1 && w <= 3) || w == 8 || w == 9) && ((v >= 2 && v <= 5) || v == 15);
  endfunction
endclass

// 18.5.4 uniqueness: scalars and the elements of an array, all pairwise different
class L18_unique;
  rand bit [2:0] a [2];
  rand bit [2:0] b, c2;
  constraint c { unique {b, a, c2}; }
  function bit check();
    bit [2:0] v [4] = '{b, a[0], a[1], c2};
    foreach (v[i]) foreach (v[j]) if (i < j && v[i] == v[j]) return 0;
    return 1;
  endfunction
endclass

// 18.5.7.1 foreach: fixed 2-D array, skipped dimension, iterator arithmetic under a guard (18.5.12)
class L18_foreach;
  rand bit [3:0] m [2][3];
  rand bit [3:0] v [4];
  constraint c {
    foreach (m[i, j]) m[i][j] < 4'd10;
    foreach (m[, j]) m[1][j] >= m[0][j];
    foreach (v[k]) if (k > 0) v[k] > v[k - 1];
  }
  function bit check();
    foreach (m[i, j]) if (m[i][j] >= 10) return 0;
    for (int j = 0; j < 3; j++) if (m[1][j] < m[0][j]) return 0;
    for (int k = 1; k < 4; k++) if (v[k] <= v[k - 1]) return 0;
    return 1;
  endfunction
endclass

// 18.4 dynamic array sized by a constraint; foreach over it; queue with size constraint
class L18_dyn;
  rand bit [3:0] d [];
  rand bit [3:0] q [$];
  constraint c {
    d.size() inside {[1:3]};
    foreach (d[i]) d[i] == i + 1;
    q.size() <= 2;
    foreach (q[i]) q[i] > 4'd10;
  }
  function bit check();
    if (!(d.size() >= 1 && d.size() <= 3) || q.size() > 2) return 0;
    foreach (d[i]) if (d[i] != i + 1) return 0;
    foreach (q[i]) if (q[i] <= 10) return 0;
    return 1;
  endfunction
endclass

// 7.12.3 reductions in constraints: the result has the element's width unless `with` widens it
class L18_reduce;
  rand bit [3:0] a [3];
  constraint c { a.sum() with (int'(item)) == 20; a.sum() < 4'd5; a.xor() != 0; a.and() == 0; a.or() > 4'd8; }
  function bit check();
    int wide = int'(a[0]) + int'(a[1]) + int'(a[2]);
    bit [3:0] narrow = a[0] + a[1] + a[2];
    return wide == 20 && narrow < 5 && (a[0] ^ a[1] ^ a[2]) != 0 && (a[0] & a[1] & a[2]) == 0 &&
           (a[0] | a[1] | a[2]) > 8;
  endfunction
endclass

// 7.12.4 item.index in a with clause
class L18_index;
  rand bit [3:0] a [3];
  constraint c { a.sum() with (int'(item) * item.index) == 7; foreach (a[i]) a[i] < 4; }
  function bit check();
    return int'(a[1]) + 2 * int'(a[2]) == 7 && a[0] < 4 && a[1] < 4 && a[2] < 4;
  endfunction
endclass

// 18.5.11 functions in constraints: single return, and a body with locals, if/else, case, a loop
class L18_func;
  rand bit [3:0] a, b, c;
  function automatic bit [3:0] twice(bit [3:0] x); return x * 2; endfunction
  function automatic bit [3:0] f(bit [3:0] x);
    bit [3:0] t = x + 1;
    if (x > 10) return t;
    case (x) 1: t = 2; 2, 5: t = 9; default: ; endcase
    for (int i = 0; i < 3; i++) t = t + 1;
    return t;
  endfunction
  constraint k { b == twice(a); c == f(a); }
  function bit check(); return b == twice(a) && c == f(a); endfunction
endclass

// 18.5.2 inheritance: a derived block overrides the same-named base block, adds its own
class L18_base_b;
  rand bit [3:0] x, y;
  constraint c1 { x < 5; }
  constraint c2 { y > 2; }
  virtual function bit check(); return x < 5 && y > 2; endfunction
endclass
class L18_inherit extends L18_base_b;
  constraint c1 { x > 10; }           // replaces the base c1
  constraint c3 { x + y < 15; }
  function bit check(); return x > 10 && y > 2 && x + y < 15; endfunction      // x + y: 32-bit (15 is an int)
endclass

// 18.5.1 external constraint blocks: explicit (extern) and implicit prototypes
class L18_extern;
  rand bit [3:0] x;
  extern constraint ce;
  constraint ci;
  function bit check(); return x > 3 && x != 6; endfunction
endclass
constraint L18_extern::ce { x > 3; }
constraint L18_extern::ci { x != 6; }

// 18.4 nested rand object: its rand fields and constraints are solved with the container
class L18_sub_n;
  rand bit [3:0] v;
  constraint cs { v != 5; }
  function bit check(); return v != 5; endfunction
endclass
class L18_nested;
  rand L18_sub_n o;
  rand bit [3:0] w;
  function new(); o = new; endfunction
  constraint c { w == o.v + 1; }
  function bit check(); return w == o.v + 1 && o.v != 5; endfunction          // o.v + 1: 32-bit
endclass

// 18.4 rand enum (named values only), packed struct with an enum member (any value), randc
class L18_enum;
  rand e_t e;
  rand ps_t p;
  randc bit [2:0] r;
  constraint c { e != E2; p.hi == 1; r != 3; }
  function bit check(); return e inside {E0, E5, E7} && p.hi == 1 && r != 3; endfunction
endclass

// 18.4 unpacked struct: only members declared rand are random (q is state)
typedef struct { rand bit [3:0] p; bit [3:0] q; } us_t;
class L18_ustruct;
  rand us_t s;
  constraint c { s.p > s.q; }
  function bit check(); return s.p > s.q; endfunction
endclass

// 18.5.12 constraint guards: a state value guards a constraint; ?: in constraints
class L18_guard;
  rand bit [3:0] x;
  bit [1:0] cfg;
  constraint c { if (cfg == 2) x < 3; x != ((cfg == 1) ? 4'd5 : 4'd6); }
  function bit check(); return !(cfg == 2 && x >= 3) && x != ((cfg == 1) ? 5 : 6); endfunction
endclass

// operators: ->, <->, shifts, part selects (constant and variable), concatenation, $countones
class L18_ops;
  rand bit [3:0] a, b;
  rand bit [1:0] k;
  constraint c {
    a[2] == 1 && a[0] == 0;
    (a[0] -> b[3]) && (b[1] <-> a[3]);
    (b >> k) != 0;
    {a[1:0], b[1:0]} != 4'hf;
    $countones(b) == 2;
    a[k] == 1;
  }
  function bit check();
    return a[2] == 1 && a[0] == 0 && (!a[0] || b[3]) && (b[1] == a[3]) && (b >> k) != 0 &&
           {a[1:0], b[1:0]} != 4'hf && $countones(b) == 2 && a[k] == 1;
  endfunction
endclass

// array elements of class objects and unpacked structs; constraints of the element class
class L18_item;
  rand bit [3:0] v;
  constraint ci { v != 7; }
  function bit check(); return v != 7; endfunction
endclass
typedef struct { rand bit [3:0] p; } ps2_t;
class L18_objarr;
  rand L18_item it [2];
  rand ps2_t ss [2];
  function new(); foreach (it[i]) it[i] = new; endfunction
  constraint c { it[0].v < it[1].v; foreach (it[i]) it[i].v > 1; ss[1].p == ss[0].p + 1; }
  function bit check();
    return it[0].v < it[1].v && it[0].v > 1 && it[1].v > 1 && it[0].v != 7 && it[1].v != 7 && ss[1].p == ss[0].p + 1;
  endfunction
endclass

// dynamic array of dynamic arrays (inner sizes per element)
class L18_dyn2;
  rand bit [3:0] dd [][];
  constraint c { dd.size() == 2; foreach (dd[i]) dd[i].size() inside {[1:2]}; foreach (dd[i, j]) dd[i][j] > i; }
  function bit check();
    if (dd.size() != 2) return 0;
    foreach (dd[i]) if (!(dd[i].size() >= 1 && dd[i].size() <= 2)) return 0;
    foreach (dd[i, j]) if (dd[i][j] <= i) return 0;
    return 1;
  endfunction
endclass

// associative arrays: constant keys, foreach over the entries (the keys the array holds), size()
class L18_assoc;
  rand bit [3:0] ai [int];
  constraint c { ai[5] > 2; foreach (ai[k]) ai[k] != 9; foreach (ai[k]) (k == 5) -> ai[k] < 12; }
  function bit check();
    if (!(ai[5] > 2 && ai[5] < 12)) return 0;
    foreach (ai[k]) if (ai[k] == 9) return 0;
    return 1;
  endfunction
endclass

// locator: find() with (...).size() counts the matching elements
class L18_find;
  rand bit [3:0] q [3];
  constraint c { q.find(x) with (x > 4'd10).size() == 1; }
  function bit check(); int n = 0; foreach (q[i]) if (q[i] > 10) n++; return n == 1; endfunction
endclass

// 18.5.13 soft: kept unless it conflicts with hard constraints (checked on free solutions); check() is
// the hard part
class L18_soft;
  rand bit [3:0] x, y;
  constraint c { soft x == 3; soft y > 8; y < 12; }
  function bit check(); return y < 12; endfunction
endclass

// 20.9 bit-vector system functions in constraints
class L18_sysfn;
  rand bit [3:0] a, b, c;
  rand bit [4:0] d;
  constraint k { $onehot(a); $onehot0(b); $countbits(c, '1) == 2; $clog2(d) == 3; }
  function bit check(); return $onehot(a) && $onehot0(b) && $countones(c) == 2 && $clog2(d) == 3; endfunction
endclass

// 18.5.12 guards on object handles: a null handle guards the constraint that would dereference it
class L18_nullg_n; rand bit [3:0] d; endclass
class L18_nullg;
  rand bit [3:0] x;
  L18_nullg_n nxt;
  constraint c { if (nxt == null) x == 5; else x != 5; }
  function bit check(); return (nxt == null) ? x == 5 : x != 5; endfunction
endclass

// 18.5.9 solve...before orders the solution (distribution) only: the legal set is unchanged
class L18_solve;
  rand bit s;
  rand bit [3:0] x;
  constraint c { s -> x == 0; solve s before x; }
  function bit check(); return !s || x == 0; endfunction
endclass

// 18.5.10 static constraint block (shared by all instances; same legal set per object)
class L18_static;
  rand bit [3:0] x;
  static constraint sc { x inside {[3:5]}; }
  function bit check(); return x >= 3 && x <= 5; endfunction
endclass

// 18.5.2 pure constraint in a virtual class, defined by the derived class
virtual class L18_pure_b;
  rand bit [3:0] x;
  pure constraint pc;
endclass
class L18_pure extends L18_pure_b;
  constraint pc { x > 12; }
  function bit check(); return x > 12; endfunction
endclass

// 18.5.13 soft constraint priorities (18.5.13.1): inline over class, derived over base, later over earlier.
// check_soft(): the soft constraints the LRM keeps for a free randomize()
class L18_softp_b;
  rand bit [3:0] x, y;
  constraint b1 { soft x == 2; soft y == 4; }
endclass
class L18_softp extends L18_softp_b;
  constraint d1 { soft x == 3; }            // derived beats base: x == 3 kept, x == 2 dropped
  constraint d2 { soft y > 6; soft y < 5; } // later beats earlier: y < 5 kept, then y > 6 conflicts: dropped;
                                            // base y == 4 is consistent with y < 5: kept
  function bit check(); return 1; endfunction
  function bit check_soft(); return x == 3 && y == 4; endfunction
endclass

// --- call sites: go() holds the randomize() call; the site's module = class + inline constraints

// 18.7 inline constraints reading caller state; 18.7.1 local:: names the caller's scope in obj.randomize()
class L18S_inline;
  rand bit [3:0] x, y;
  bit [3:0] lim, off;
  constraint c { x < 12; }
  function bit go();
    L18S_inline o = this;
    return o.randomize() with { x > local::off; y == x + local::off; x < lim; };
  endfunction
  function bit check(); return x < 12 && x > off && y == x + off && x < lim; endfunction
endclass

// 18.11 randomize(a): only the listed variables are random, the others hold their values
class L18S_args;
  rand bit [3:0] a, b;
  constraint c { a > b; }
  function bit go(); return randomize(a); endfunction
  function bit check(); return a > b; endfunction
endclass

// 18.8 rand_mode(0): the field holds its value, constraints still apply; 18.9 constraint_mode(0)
class L18S_modes;
  rand bit [3:0] a, b;
  constraint ca { a > 5; }
  constraint cb { b < 3; }
  function bit go(); b.rand_mode(0); cb.constraint_mode(0); return randomize(); endfunction
  function bit check(); return a > 5; endfunction
endclass

// 18.5.13.1 inline soft beats class soft; 18.5.13.2 disable soft
class L18S_softi;
  rand bit [3:0] x, y;
  constraint c { soft x == 1; soft y == 2; }
  function bit go(); return randomize() with { soft x == 7; disable soft y; y > 9; }; endfunction
  function bit check(); return y > 9; endfunction
  function bit check_soft(); return x == 7; endfunction
endclass
