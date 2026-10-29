// Parity harness for plant_v1.hpp.
// usage: plant_v1_harness params.txt inputs.csv outputs.csv
// params.txt: whitespace-separated: act_mode, init[5], hx[23], n_knots, knots[n], xk[n], mix[3], slab[10], room[3]
// inputs.csv: header + rows tb,bp,pmod,ww,p1,p2,p3,pos,tout
#include "plant_v1.hpp"
#include <cstdio>
#include <fstream>
#include <sstream>
#include <string>

int main(int argc, char** argv) {
  if (argc != 4) { std::fprintf(stderr, "usage\n"); return 2; }
  std::ifstream pf(argv[1]);
  hpn_plant::Params P{};
  int mode; double init[5];
  pf >> mode;
  for (double& v : init) pf >> v;
  for (double& v : P.hx) pf >> v;
  pf >> P.n_knots;
  for (int i = 0; i < P.n_knots; ++i) pf >> P.knots[i];
  for (int i = 0; i < P.n_knots; ++i) pf >> P.xk[i];
  for (double& v : P.mix) pf >> v;
  for (double& v : P.slab) pf >> v;
  for (double& v : P.room) pf >> v;
  if (!pf) { std::fprintf(stderr, "bad params\n"); return 3; }
  hpn_plant::Plant plant(P, mode, init);
  std::ifstream in(argv[2]);
  std::FILE* out = std::fopen(argv[3], "w");
  std::string line;
  std::getline(in, line);
  std::fprintf(out, "tinc_sensor,tinc_water,tfwd_sensor,tret,slab,room,pos,mode\n");
  while (std::getline(in, line)) {
    std::stringstream ss(line);
    std::string c;
    double v[9];
    for (int i = 0; i < 9; ++i) { std::getline(ss, c, ','); v[i] = std::stod(c); }
    hpn_plant::Inputs x{v[0], v[1], v[2], v[3], v[4] > 0.5, v[5] > 0.5, v[6] > 0.5, v[7], v[8]};
    auto o = plant.step(x);
    std::fprintf(out, "%.17g,%.17g,%.17g,%.17g,%.17g,%.17g,%.17g,%d\n", o.tinc_sensor, o.tinc_water,
                 o.tfwd_sensor, o.tret, o.slab, o.room, o.pos, o.hx_mode);
  }
  std::fclose(out);
  return 0;
}
