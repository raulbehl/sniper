package ra_pkg;   // fvgen regressions from real testbenches; fv_regress_b.sv reuses names and lines
  class dup;
    rand bit [3:0] y;
    constraint c { y < 12; }
    covergroup cg; coverpoint y; endgroup
    function new(); cg = new(); endfunction
  endclass
  class seq;
    task body(); dup d = new(); void'(d.randomize() with { y > 1; }); endtask
  endclass

  // `uvm_do_with(req, {}): an empty inline constraint block
  class empty_inline;
    task body(); dup d = new(); void'(d.randomize() with {}); endtask
  endclass

  // unlabelled coverpoint written over two lines, ignore_bins removed from automatic bins,
  // [N] bins over a 32-bit range
  class cov_c;
    rand bit [3:0] y;
    rand bit [31:0] n32;
    covergroup cg;
      coverpoint
          y { ignore_bins ig = {[0:7]}; }
      cp_n : coverpoint n32 { bins b[4] = {[0:$]}; }
    endgroup
    function new(); cg = new(); endfunction
  endclass

  // size() must exceed --max-array 8; the sum needs more than the 8 modelled elements
  class arr_c;
    rand bit [7:0] q[];
    constraint c_sz { q.size() inside {[20:30]}; }
    constraint c_el { foreach (q[i]) q[i] < 10; }
    constraint c_sum { q.sum() with (int'(item)) == 150; }
  endclass

  // LRM 18.5.13: a hard constraint beats a soft one; derived soft beats base soft
  class sb;
    rand bit [3:0] m, k, p;
    constraint c_s { soft m == 0; soft k == 5; soft p == 1; }
  endclass
  class sd extends sb;
    constraint c_h { m == 1; }
    constraint c_h2 { soft p == 2; }
  endclass
  // a soft constraint that conflicts only for some testbench state
  class st_c;
    int lim;
    rand bit [3:0] z;
    constraint c { z < lim; soft z == 3; }
  endclass

  // this.randomize() is a plain randomize(), not randomize(this)
  class tr_c;
    rand bit [3:0] w;
    constraint c { w == 3; }
    function void go(); void'(this.randomize()); endfunction
  endclass

  // constraint_mode / rand_mode before the call, and in the constructor
  class mode_u;
    task run();
      dup d = new();
      d.c.constraint_mode(0);
      void'(d.randomize());
    endtask
    task run2();
      dup d = new();
      d.y.rand_mode(0);
      void'(d.randomize());
    endtask
  endclass
  class cm_c;
    rand bit [3:0] v;
    constraint c_off { v == 1; }
    constraint c_on { v < 8; }
    function new(); c_off.constraint_mode(0); endfunction
  endclass

  // unsatisfiable: the report names the conflicting items (c1, c2), not c3
  class bad;
    rand bit [3:0] b;
    constraint c1 { b > 10; }
    constraint c2 { b < 5; }
    constraint c3 { b != 7; }
  endclass

  // inline constraints reading the caller's variables; a method of another object; std::randomize
  class lim_c;
    int unsigned k;
    function int unsigned get_k(); return k; endfunction
  endclass
  class user_c;
    rand bit [3:0] y2;
    lim_c l;
    constraint c { y2 < l.get_k(); }
    task body();
      int lim = 5;
      bit [3:0] v;
      dup d = new();
      void'(d.randomize() with { y < lim; });
      void'(std::randomize(v) with { v < 3; });
    endtask
  endclass
  // cross ignore/illegal bins with a `with` filter: a product all of whose value combinations pass
  // is not a bin (ibex exception_stall_instr_cross); one where only some do keeps the rest
  class xw_c;
    rand bit [1:0] a;
    rand bit b;
    rand bit [2:0] c;
    covergroup cg;
      cp_a : coverpoint a;
      cp_b : coverpoint b;
      cp_c : coverpoint c { bins lo = {[0:3]}; bins hi = {[4:7]}; }
      x : cross cp_a, cp_b {
        illegal_bins il = binsof(cp_a) intersect {[0:1]} with (cp_b == 1);
        ignore_bins ig = binsof(cp_a) intersect {[2:3]} with (cp_a + cp_b == 3);
      }
      y : cross cp_c, cp_b { ignore_bins p = binsof(cp_c.lo) with (cp_c < 2); }
    endgroup
    function new(); cg = new(); endfunction
  endclass
  // LRM 19.5.7: bin values outside the coverpoint type are removed, ranges clipped (core-v-verif
  // uvma_cvxif cg_request cp_rs_valid on a 2-bit field; the LRM's own p1 example)
  class vr_c;
    localparam int W = 32;
    rand bit [1:0] rs;
    rand bit signed [2:0] p1;
    covergroup cg;
      cp_rs : coverpoint rs {
        ignore_bins ign = {3'b111} with (1 == 1);
        bins v2 = {2'b11};
        bins v3 = {3'b111};
      }
      cp_p1 : coverpoint p1 { bins b1 = {1, [2:5], [6:10]}; bins b2 = {-1, [1:10], 15}; }
      // a constant iff (cva6 uvme_axi_covg: ignore_bins IGN_SIZE3 = {3} iff (XLEN == 32))
      cp_k : coverpoint rs { bins v[] = {[0:3]}; ignore_bins i3 = {3} iff (W == 32); ignore_bins i0 = {0} iff (W == 64); }
    endgroup
    function new(); cg = new(); endfunction
  endclass
  // dead constraint guards: g_site only at a call whose inline constraints exclude it (expected,
  // not reported), g_cls in the class itself (reported)
  class dg_c;
    rand bit [3:0] k, v;
    constraint g_site { (k == 1) -> v > 3; }
    constraint g_cls { (k == 9) -> v == 0; k < 8; }
    task body(); void'(this.randomize() with { k == 0; }); endtask
  endclass
endpackage
