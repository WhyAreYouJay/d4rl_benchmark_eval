import torch
import torch.nn.functional as F
from torch.nn import Linear, Module
from torch import nn

def g(x):
    return torch.where(x >= 0, x + 0.5, torch.sigmoid(x))

#@torch.compile
def log_g(x):
    return torch.where(x >= 0, (F.relu(x) + 0.5).log(), -F.softplus(-x))

#@torch.compile
def parallel_scan_log(log_coefficients, log_values):
    # log_coefficients: (batch_size, device, input_size)
    # log_values: (batch_size, device + 1, input_size)
    a_star = F.pad(torch.cumsum(log_coefficients, dim=1), (0, 0, 1, 0))
    log_h0_plus_b_star = torch.logcumsumexp(log_values - a_star, dim=1)
    log_h = a_star + log_h0_plus_b_star
    return torch.exp(log_h)[:, 1:]

class Conv1dLayer(Module):
    def __init__(self, dim, kernel_size):
        super().__init__()
        self.kernel_size = kernel_size
        self.net = nn.Conv1d(dim, dim, kernel_size = kernel_size, groups = dim)
    #@torch.compile()
    def forward(self, x):
        x = x.transpose(1, 2) # b n d -> b d n
        x = F.pad(x, (self.kernel_size - 1, 0), value = 0.)
        x = self.net(x)
        return x.transpose(1, 2) # b d n -> b n d
    
    
class minLSTM(nn.Module):
    def __init__(self, dim,batch_size, device, expansion_factor = 1., dropout = 0.):
        super(minLSTM, self).__init__()
        self.dim = dim
        #self.input_shape = input_shape
        self.dim=dim
        self.exp_dim = int(dim * expansion_factor)
        self.log_h = log_g(torch.zeros((batch_size, 1, self.exp_dim), device = device))
        self.batch_size = batch_size
        # Initialize the linear layers for the forget gate, input gate, and hidden state transformation
        self.linear = nn.Linear(self.dim, 3*self.exp_dim, device = device)
        self.drop_f = nn.Dropout(dropout)
        self.linear_i = nn.Linear(self.dim, self.exp_dim, device = device)
        self.linear_h = nn.Linear(self.dim, self.exp_dim, device = device)
        self.down_projection = Linear(self.exp_dim,dim, bias=False, device = device) if expansion_factor != 1.0 else None
        self.drop_proj = nn.Dropout(dropout)
    
    def eval_mode(self):
        self.log_h = log_g(torch.zeros((1, 1, self.exp_dim), device = self.log_h.device))
        
    def train_mode(self):
        self.log_h = log_g(torch.zeros((self.batch_size, 1, self.exp_dim), device = self.log_h.device))
    
    def forward(self, x_t):
        """
        pre_h: (batch_size, units) - previous hidden state (h_prev)
        x_t: (batch_size, input_size) - input at time step t
        """
        # Forget gate: log_f_t = log(sigmoid(W_f * x_t))
        k_f, k_i, k_h = self.drop_f(self.linear(x_t)).chunk(3, dim = -1)
        log_f = -F.softplus(-k_f) # (batch_size, units)
        log_i = -F.softplus(-k_i)


        # Hidden state: log_tilde_h_t = log(W_h * x_t)
        log_tilde_h = log_g(k_h)  # (batch_size, units)
        
        
        # Use parallel_scan_log to compute the hidden state
        h_t = parallel_scan_log(log_f, torch.cat([self.log_h, log_i + log_tilde_h], dim=1))

        if self.down_projection is not None:
            h =  self.down_projection(h_t)
        else:
            h = h_t
        return self.drop_proj(h)

  
class CausalDepthWiseConv1d(Module):
    def __init__(self, dim, kernel_size, device):
        """Simple sequential CONV1D net"""
        super().__init__()
        self.kernel_size = kernel_size
        self.net = nn.Sequential(
            nn.Conv1d(dim, dim, kernel_size = kernel_size, groups = dim, device = device),
            nn.Conv1d(dim, dim, kernel_size = 1, device = device)
        )
    #@torch.compile
    def forward(self, x):
        x = x.transpose(1, 2) # b n d -> b d n
        x = F.pad(x, (self.kernel_size - 1, 0), value = 0.)
        x = self.net(x)
        return x.transpose(1, 2) # b d n -> b n d    
    
class minLSTMCell(Module):
    """This Version corresponds to what has been done in https://github.com/lucidrains/minGRU-pytorch/"""
    def __init__(self,dim,drop_p,kernel_size,expansion_factor,batch_size,device, conv, mult=4):
        """This Version corresponds to what has been done in https://github.com/lucidrains/minGRU-pytorch/"""
        super().__init__()
        self.conv = CausalDepthWiseConv1d(dim, kernel_size, device = device) if conv else None #Conv1dLayer(dim,kernel_size)
        self.ln1 = torch.nn.LayerNorm(dim, device = device)
        self.cell = minLSTM(dim,batch_size,device,expansion_factor, drop_p)
        self.ln2 = torch.nn.LayerNorm(dim, device = device)
        self.mlp = nn.Sequential(
                nn.Linear(dim, int(mult * dim), device = device),
                nn.GELU(),#Reinformer uses GELU
                nn.Linear(int(mult * dim), dim, device = device),
                nn.Dropout(drop_p),
            )
        self.ln3 = torch.nn.LayerNorm(dim, device = device)
    #@torch.compile
    def forward(self,x):
        residual = x
        if self.conv is not None:
            x = self.conv(self.ln1(x)) + residual
            residual = x
        x = self.cell(self.ln2(x)) + residual
        residual = x
        return self.mlp(self.ln3(x)) + residual

class minLSTMBlock(Module):
    """This Version corresponds to what has been done in https://github.com/cheind/mingru/blob/main/mingru/modules.py"""
    def __init__(self,dim,drop_p,kernel_size,expansion_factor,batch_size,device, conv, n_layers, mult=4):
        """This Version corresponds to what has been done in https://github.com/cheind/mingru/blob/main/mingru/modules.py"""
        super().__init__()
        self.conv = CausalDepthWiseConv1d(dim, kernel_size, device = device) if conv else None #Conv1dLayer(dim,kernel_size)
        self.cells = []
        self.lns = []
        for i in range(n_layers):
            self.cells.append(minLSTM(dim,batch_size,device,expansion_factor, drop_p))
            self.lns.append(nn.LayerNorm(dim, device = device))
        self.cells = nn.ModuleList(self.cells)
        self.lns = nn.ModuleList(self.lns)
        self.mlp = nn.Sequential(
                nn.Linear(dim, int(mult * dim), device = device),
                nn.GELU(),#Reinformer uses GELU
                nn.Linear(int(mult * dim), dim, device = device),
                nn.Dropout(drop_p),
            ) 
        self.ln3 = torch.nn.LayerNorm(dim, device = device)
        self.ln1 = torch.nn.LayerNorm(dim, device = device)
    #@torch.compile
    def forward(self,x):    
        residual = x
        if self.conv is not None:
            x = self.conv(self.ln1(x)) + residual
            residual = x
        for i, cell in enumerate(self.cells):
            x = cell(self.lns[i](x)) + residual
        residual = x
        return self.mlp(self.ln3(x))