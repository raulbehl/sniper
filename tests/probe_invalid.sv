// Code commercial simulators accept but slang does not bind: reported as facts.invalid, not analysed.
class h_c;
  int v;
endclass

module invalid_top;
  h_c h;
  int x;
  initial begin
    x = 1;
    if (h && x) x = 2;
  end
endmodule
