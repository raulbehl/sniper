// Constructs that broke on real testbenches (ibex, OpenTitan, core-v-verif, riscv-dv).
interface pif_p #(parameter int W = 8) (input logic clk);
  logic [W-1:0] data;
endinterface

package corpus_pkg;
  typedef struct { bit [7:0] a; bit [7:0] b; } pair_t;

  class item;
    rand bit [7:0] x;
    rand int unsigned d;
    pair_t p;
    constraint c_w { d dist {0 :/ 1, [1:9] :/ 4, 10 := 2}; }
    covergroup cg;
      cp : coverpoint x { bins hi = {[128:$]}; }
    endgroup
    function new(); cg = new(); endfunction
    function void set_pair(); p.a = 1; endfunction
  endclass

  class drv;
    virtual pif_p #(16) vif;
    item it;
    function void fill(ref pair_t v[]);
      v[0].b = 8'h5;
    endfunction
    task run();
      // a comment right before the call must not end up in its source text
      void'(this.randomize());
      vif.data <= 16'h1;
      it.p.b = 3;
    endtask
  endclass
endpackage

module corpus_top #(parameter bit EN = 0);
  logic clk = 0;
  pif_p #(16) pif (.clk(clk));
  if (EN) begin : g_off
    initial pif.data = 0;
  end
endmodule

class ext_c;
  rand int a;
  extern constraint e_c;
  extern static constraint s_c;
endclass
constraint ext_c::e_c {
  a < 10;
}
static constraint ext_c::s_c { a != 3; }

// parameterized class nothing specializes: reported through its default specialization
class gen_c #(int N = 4);
  rand int unsigned v;
  constraint c_n { v < N; }
  function int twice(); return 2 * v; endfunction
endclass

// specialized only through a typedef used as a member type
class pred_c #(type CFG_T);
  CFG_T cfg;
  function void predict(); endfunction
endclass
class env_c;
  typedef pred_c #(.CFG_T(corpus_pkg::item)) pred_t;
  pred_t pred;
endclass
