import torch
import torch.nn.functional as F
from torch.nn import Linear, Module
from torch import nn

def g(x):
    return torch.where(x >= 0, x + 0.5, torch.sigmoid(x))

@torch.compile
def log_g(x):
    return torch.where(x >= 0, (F.relu(x) + 0.5).log(), -F.softplus(-x))
    x_neg = x < 0
    lg = (F.relu(x) + 0.5).log()
    lg[x_neg] = -F.softplus(-x)[x_neg]
    return lg

@torch.compile
def parallel_scan_log(log_coefficients, log_values):
    # log_coefficients: (batch_size, device, input_size)
    # log_values: (batch_size, device + 1, input_size)
    a_star = F.pad(torch.cumsum(log_coefficients, dim=1), (0, 0, 1, 0))
    log_h0_plus_b_star = torch.logcumsumexp(log_values - a_star, dim=1)
    log_h = a_star + log_h0_plus_b_star
    del log_coefficients,log_values
    torch.cuda.empty_cache()
    return torch.exp(log_h)[:, 1:]

class Conv1dLayer(Module):
    def __init__(self, dim, kernel_size):
        super().__init__()
        self.kernel_size = kernel_size
        self.net = nn.Conv1d(dim, dim, kernel_size = kernel_size, groups = dim)
    def forward(self, x):
        x = x.transpose(1, 2) # b n d -> b d n
        x = F.pad(x, (self.kernel_size - 1, 0), value = 0.)
        x = self.net(x)
        return x.transpose(1, 2) # b d n -> b n d

class minGRU(Module):
    def __init__(self, dim,batch_size, device, expansion_factor = 1.):
        super().__init__()
        self.dim=dim
        self.exp_dim = int(dim * expansion_factor)
        self.log_h = log_g(torch.zeros((batch_size, 1, self.exp_dim), device = device))
        self.f = Linear(dim, 2*self.exp_dim, bias=False)
        self.down_projection = Linear(self.exp_dim,dim, bias=False) if expansion_factor != 1 else nn.Identity()
        # output of f_z can be viewed as the proportion of the info from the current timestep that is incorporated into
        # the next hidden state (for more info see paper "Were RNNs All We Needed?")

        # This code is also available in the paper "Were RNNs All We Needed?"
        # We could still change the code to our specifications for this project
        # however original code is already written extremely cleanly and i see no reason to
        # change names, rearrange code etc. for the sake of it

        """
        Note: This version is not in log-space
        Sequential Algorithm for minGRU:
                z_t ← σ(f_z(x_t))
                h˜_t ← g(f_h(x_t))
                h_t ← (1 − z_t) ⊙ h_{t−1} + z_t ⊙ h˜_t
        """

        # We use the log-space version of the algorithm for additional numerical stability
        # (i.e. long sequences more likely to result in numerical underflow)
        # by e.g. converting to log-values, summing and then exponentiating we achieve the same result as
        # multiplying the original values but with better numerical stability
    @torch.compile
    def forward(self, x:torch.Tensor, h0=None):
        # x: (batch_size, device, input_size)
        # h_0: (batch_size, 1, hidden_size)
        k,h_x = self.f(x).chunk(2,dim = -1)
        log_z = -F.softplus(-k)
        log_coeffs = -F.softplus(k)
        log_tilde_h = log_g(h_x)
        return self.down_projection(parallel_scan_log(log_coeffs, torch.cat([self.log_h,log_tilde_h + log_z], dim=1)))
    
class CausalDepthWiseConv1d(Module):
    def __init__(self, dim, kernel_size):
        """Simple sequential CONV1D net"""
        super().__init__()
        self.kernel_size = kernel_size
        self.net = nn.Sequential(
            nn.Conv1d(dim, dim, kernel_size = kernel_size, groups = dim),
            nn.Conv1d(dim, dim, kernel_size = 1)
        )
    @torch.compile
    def forward(self, x):
        x = x.transpose(1, 2) # b n d -> b d n
        x = F.pad(x, (self.kernel_size - 1, 0), value = 0.)
        x = self.net(x)
        return x.transpose(1, 2) # b d n -> b n d    
    
class BlockV1(Module):
    """This Version is what I understand to be one block in the minGRU architecture"""
    def __init__(self,dim,n_layers,drop_p,kernel_size,expansion_factor,batch_size,device, mult=4): 
        """This Version is what I understand to be one block in the minGRU architecture"""
        super().__init__()
        self.n_layers = n_layers
        self.conv = CausalDepthWiseConv1d(dim, kernel_size) #Conv1dLayer(dim,kernel_size)
        self.ln1 = torch.nn.LayerNorm(dim)
        self.mingru_layers =[]
        for _ in range(n_layers):
            m = minGRU(dim,batch_size,device,expansion_factor)
            #m.compile()
            self.mingru_layers += [m, torch.nn.LayerNorm(dim)]
        self.mod_l = nn.Sequential(*self.mingru_layers)
        self.ln2 = torch.nn.LayerNorm(dim)
        self.mlp = nn.Sequential(
            nn.Linear(dim, mult * dim),
            nn.ReLU(),#Reinformer uses GELU
            nn.Linear(mult * dim, dim),
            nn.Dropout(drop_p),
        )
    def forward(self,x): 
        x = self.conv(x)
        minGRU_input = self.ln1(x)
        for i in range(self.n_layers):
            x = self.mingru_layers[2*i](minGRU_input) + x #Residuals!
            minGRU_input = self.mingru_layers[2*i+1](x) # LayerNorm before next layer
        mlp_input = self.ln2(minGRU_input) #Continue with the MLP
        x = self.mlp(mlp_input)+x#Residuals!
        return x


class BlockV2(Module):
    """This Version corresponds to what has been done in https://github.com/lucidrains/minGRU-pytorch/"""
    def __init__(self,dim,n_layers,drop_p,kernel_size,expansion_factor,batch_size,device, mult=4):
        """This Version corresponds to what has been done in https://github.com/lucidrains/minGRU-pytorch/"""
        super().__init__()
        self.seq = []
        self.module_list = []
        self.min_grus = []
        for i in range(n_layers):
            self.conv = CausalDepthWiseConv1d(dim, kernel_size) #Conv1dLayer(dim,kernel_size)
            self.ln1 = torch.nn.LayerNorm(dim)
            self.min_gru = minGRU(dim,batch_size,device,expansion_factor)
            self.ln2 = torch.nn.LayerNorm(dim)
            self.mlp = nn.Sequential(
                nn.Linear(dim, mult * dim),
                nn.ReLU(),#Reinformer uses GELU
                nn.Linear(mult * dim, dim),
                nn.Dropout(drop_p),
            )
            self.min_grus.append(self.min_gru)
            self.seq = self.seq + [self.conv, self.ln1, self.min_gru, self.ln2, self.mlp]
        self.st = nn.Sequential(self.seq[:2])
        self.end = nn.Sequential(self.seq[-2:])
        self.mid_list = []
        for i in range(n_layers -1):
            self.mid_list.append(nn.Sequential(self.seq[3 + 5*i:6 + 5*i]))
        self.seq_nn = nn.Sequential(*self.seq)
            
    def forward(self,x):
        x = self.st(x)
        x = self.min_grus[0](x) + x
        for i,s in enumerate(self.mid_list):
            x = s(x)
            x = self.min_grus[i+1](x) + x
        return self.end(x)
    
    
class BlockV3(Module):
    """This Version corresponds to what has been done in https://github.com/lucidrains/minGRU-pytorch/"""
    def __init__(self,dim,n_layers,drop_p,kernel_size,expansion_factor,batch_size,device, mult=4):
        """This Version corresponds to what has been done in https://github.com/lucidrains/minGRU-pytorch/"""
        super().__init__()
        self.conv = CausalDepthWiseConv1d(dim, kernel_size) #Conv1dLayer(dim,kernel_size)
        self.ln1 = torch.nn.LayerNorm(dim)
        self.min_gru = minGRU(dim,batch_size,device,expansion_factor)
        self.ln2 = torch.nn.LayerNorm(dim)
        self.mlp = nn.Sequential(
                nn.Linear(dim, mult * dim),
                nn.ReLU(),#Reinformer uses GELU
                nn.Linear(mult * dim, dim),
                nn.Dropout(drop_p),
            )
    @torch.compile
    def forward(self,x):
        residual = x
        x = self.conv(x) + residual
        x = self.ln1(x)
        x = self.min_gru(x) + residual
        x = self.ln2(x)
        return self.mlp(x)
