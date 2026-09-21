module basic_gates (
    input  logic a,
    input  logic b,

    output logic and_y,
    output logic or_y,
    output logic not_y,
    output logic nand_y,
    output logic nor_y,
    output logic xor_y,
    output logic xnor_y
);

    assign and_y  = a & b;
    assign or_y   = a | b;
    assign not_y  = ~a;
    assign nand_y = ~(a & b);
    assign nor_y  = ~(a | b);
    assign xor_y  = a ^ b;
    assign xnor_y = ~(a ^ b);

`ifdef COCOTB_SIM

    initial begin
        $dumpfile("wave/basic_gates.vcd");
        $dumpvars(0, basic_gates);
    end

`endif

endmodule