// What fvgen needs from sniper: expression widths/signedness, packed-struct member offsets,
// enum/parameter constants, type_info, coverage options, transition bins, functions called from
// constraints, constraint_mode/rand_mode calls, unlabelled coverpoint names.
// fv_ext_pkg (ext_lim) is written by the test outside --own.
package fv_pkg;
  parameter int LIM = 12;
  typedef enum logic [1:0] {RD = 0, WR = 1, AT = 3} op_e;
  typedef struct packed { logic [3:0] hi; op_e op; logic lo; } ps_t;

  function automatic int unsigned twice(int unsigned v);
    return 2 * v;
  endfunction

  class item;
    rand bit [7:0] a;
    rand bit signed [7:0] sa;
    rand op_e op;
    rand ps_t ps;
    rand bit [3:0] q[$];
    rand bit [3:0] fx[4];
    rand int unsigned n;
    constraint c_w { a + sa > 8'd3; ps.op == WR; n < LIM; }
    constraint c_f { n == twice(a[1:0]); }
    constraint c_off { q.size() < 3; }
    constraint c_ext { a != fv_ext_pkg::ext_lim(2); }

    covergroup cg;
      option.per_instance = 1;
      cp_a : coverpoint a {
        option.at_least = 2;
        bins t2 = (0 => 1), (2 => 3);
        bins tr = (1 [*2:3] => [4:5] => 6 [-> 1] => 7 [= 2]);
      }
      coverpoint
          op;
    endgroup
    function new(); cg = new(); endfunction
  endclass

  class user;
    item it;
    task run();
      it = new();
      it.c_off.constraint_mode(0);
      it.n.rand_mode(0);
      it.constraint_mode(1);
      if (!it.randomize()) $error("randomize");
      it.rand_mode(0);
    endtask
  endclass
endpackage

module fv_top;
  import fv_pkg::*;
  initial begin
    user u = new();
    u.run();
  end
endmodule
