package rb_pkg;   // same class/covergroup names as fv_regress_a.sv, randomize() on the same line
  class dup;
    rand bit [3:0] y;
    constraint c { y > 2; }
    covergroup cg; coverpoint y; endgroup
    function new(); cg = new(); endfunction
  endclass
  class seq;
    task body(); dup d = new(); void'(d.randomize() with { y < 9; }); endtask
  endclass
endpackage

module fv_regress_top;
  initial begin
    ra_pkg::seq a = new();
    rb_pkg::seq b = new();
  end
endmodule
